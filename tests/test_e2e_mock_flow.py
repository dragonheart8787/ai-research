"""End-to-end mock flow: Research Request -> Planning -> Approval -> Execution -> Review -> Completion.

All five adapters are mocks. The test forbids network sockets and subprocesses for its whole duration."""
from __future__ import annotations

import socket
import subprocess

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import (
    ApprovalRequirement,
    ApprovalType,
    ArtifactClass,
    ExpectedOutput,
    ResourceLimits,
    Role,
    TaskKind,
    TrustLevel,
)
from airos.domain.states import Event, TaskState
from airos.errors import ActorNotAllowed, GuardFailed
from airos.persistence.sqlite_repo import SYSTEM, SqliteRepository
from tests.conftest import DIRECTOR, EXECUTOR, HUMAN, PLANNER, free_contract, make_contract

ROLES = [r.value for r in Role]


@pytest.fixture(autouse=True)
def no_network_no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network/subprocess access is forbidden in the mock flow")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _world(repo, tmp_path, executor_script=None):
    adapters = all_mock_adapters()
    if executor_script is not None:
        adapters[Role.EXECUTOR] = MockAdapter(Role.EXECUTOR, script=executor_script)
    reg = AgentRegistry([AgentSpec(role=r) for r in ROLES], adapters, {})
    svc = ArtifactService(repo, ArtifactStore(tmp_path / "cas", tmp_path / "eval"))
    return Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), svc, "worker-1"), svc


def _plan_and_approve(repo, task, *, needs_human: bool):
    t = repo.transition(task.task_id, Event.PLAN, PLANNER, task.version, GuardContext(plan_ok=True))
    if not needs_human:
        return repo.transition(t.task_id, Event.AUTO_APPROVE, SYSTEM, t.version)
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    with pytest.raises(ActorNotAllowed):                       # an agent can never approve
        repo.transition(t.task_id, Event.APPROVE, DIRECTOR, t.version)
    for req in t.contract.approval_requirements:
        repo.decide_approval(repo.request_approval(t.task_id, req.type, SYSTEM).approval_id, True, HUMAN, "approved")
    return repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)


def _research_contract():
    return make_contract(
        assigned_role=Role.CHATGPT_WORK, resource_limits=ResourceLimits(max_usd_micro=0), allowed_actions=[],
        approval_requirements=[ApprovalRequirement(type=ApprovalType.EXTERNAL_HANDOFF, reason="data leaves the host")],
        expected_output=[ExpectedOutput(logical_name="notes.md", media_type="text/markdown",
                                        artifact_class=ArtifactClass.EXTERNAL_HANDOFF)])


def _analysis_contract():
    return make_contract(expected_output=[ExpectedOutput(logical_name="result.md", media_type="text/markdown",
                                                         artifact_class=ArtifactClass.WORK_PRODUCT)])


def test_full_mock_flow_request_to_completion(repo, tmp_path):
    worker, svc = _world(repo, tmp_path)
    # 1. Research Request (human)
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR,
                                          research_objective="[MOCK] investigate topic X"), HUMAN).task
    assert root.state is TaskState.DRAFT
    # 2. Planning (planner): two children and a dependency
    research = repo.create_task(_research_contract(), PLANNER, root.task_id).task
    analysis = repo.create_task(_analysis_contract(), PLANNER, root.task_id).task
    repo.add_dependency(analysis.task_id, research.task_id, PLANNER)
    # 3. Approval (humans only)
    root = _plan_and_approve(repo, root, needs_human=False)
    research = _plan_and_approve(repo, repo.get_task(research.task_id), needs_human=True)
    analysis = _plan_and_approve(repo, repo.get_task(analysis.task_id), needs_human=True)
    assert {t.state for t in (root, research, analysis)} == {TaskState.APPROVED}
    # 4. Execution
    for _ in range(3):
        worker.tick()
    research, analysis = repo.get_task(research.task_id), repo.get_task(analysis.task_id)
    assert research.state is TaskState.REVIEW and analysis.state is TaskState.APPROVED   # analysis waits for its dependency
    # 5. Review: ChatGPT-Work output (untrusted) needs a HUMAN reviewer; the Director may not accept it
    with pytest.raises(GuardFailed, match="human reviewer"):
        repo.transition(research.task_id, Event.ACCEPT, DIRECTOR, research.version)
    research = repo.transition(research.task_id, Event.ACCEPT, HUMAN, research.version)
    assert research.state is TaskState.COMPLETED
    for _ in range(3):
        worker.tick()
    analysis = repo.get_task(analysis.task_id)
    assert analysis.state is TaskState.REVIEW
    with pytest.raises(ActorNotAllowed):                      # the producing executor cannot accept its own output
        repo.transition(analysis.task_id, Event.ACCEPT, EXECUTOR, analysis.version)
    analysis = repo.transition(analysis.task_id, Event.ACCEPT, DIRECTOR, analysis.version)
    assert analysis.state is TaskState.COMPLETED
    # 6. Completion: the objective finishes only after every child completed, then a human accepts it
    for _ in range(3):
        worker.tick()
    root = repo.get_task(root.task_id)
    assert root.state is TaskState.REVIEW
    with pytest.raises(GuardFailed, match="human reviewer"):
        repo.transition(root.task_id, Event.ACCEPT, DIRECTOR, root.version)
    root = repo.transition(root.task_id, Event.ACCEPT, HUMAN, root.version)
    assert root.state is TaskState.COMPLETED

    # ---- post-conditions
    arts = {a.logical_name: a for a in (repo.get_artifact(r[0]) for r in repo._conn().execute("SELECT artifact_id FROM artifacts"))}
    assert set(arts) == {"notes.md", "result.md"} and all(a.is_mock for a in arts.values())
    assert arts["notes.md"].trust_level is TrustLevel.UNTRUSTED          # ChatGPT Work output is untrusted
    assert arts["result.md"].trust_level is TrustLevel.TRUSTED
    assert all(svc.verify(a.artifact_id).ok for a in arts.values())
    res = svc.verify_all()                                              # PROPOSAL M3: provenance recomputes for 100% of artifacts
    assert set(res) == {a.artifact_id for a in arts.values()} and all(r.ok for r in res.values())
    ledger = repo.list_ledger()
    reserves = {e.entry_id for e in ledger if e.kind.value == "reserve"}
    assert reserves and sorted(e.reserve_ref for e in ledger if e.kind.value == "release") == sorted(reserves)  # all closed
    assert repo.verify_chain().ok and repo.verify_chain_with_anchors().ok
    anchors = repo.anchor_path.read_text().splitlines()
    assert anchors[-1].split("\t")[3] == "task.transition" and anchors[-1].split("\t")[2] == repo.head().entry_hash
    types = [e.event_type for e in repo.iter_events()]
    for needed in ("task.created", "approval.granted", "budget.reserved", "run.started", "budget.settled", "artifact.registered"):
        assert needed in types


def test_state_and_chain_survive_restart_midway(repo, open_repo, tmp_path, db_path, clock):
    worker, _ = _world(repo, tmp_path)
    t = _plan_and_approve(repo, repo.create_task(_analysis_contract(), HUMAN).task, needs_human=True)
    worker.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW
    head = repo.head()
    repo.close()
    again = SqliteRepository(db_path, clock=clock, anchor_path=repo.anchor_path)
    assert again.get_task(t.task_id).state is TaskState.REVIEW and again.head() == head
    assert again.verify_chain_with_anchors().ok
    again.close()


# ------------------------------------------------------------------ E2E-02: crashes


def test_adapter_crash_is_retried_then_succeeds(repo, tmp_path, clock):
    worker, _ = _world(repo, tmp_path, executor_script=lambda r: "crash" if r.attempt == 1 else "success")
    t = _plan_and_approve(repo, repo.create_task(_analysis_contract(), HUMAN).task, needs_human=True)
    worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.QUEUED and cur.error_history[-1].error_class == "adapter_error"
    clock.advance(Limits().backoff_ms(1))
    worker.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW
    assert [r.state.value for r in repo.list_runs(t.task_id)] == ["failed", "succeeded"]


def test_process_death_mid_run_is_recovered_by_a_new_worker_without_duplicate_runs(repo, tmp_path, clock):
    w1, _ = _world(repo, tmp_path / "a")
    t = _plan_and_approve(repo, repo.create_task(_analysis_contract(), HUMAN).task, needs_human=True)
    t = repo.queue_task(t.task_id, t.version)
    dead = repo.start_run(t.task_id, "worker-1", "executor-1", t.version)       # worker-1 "dies" here: no heartbeat, no result
    w2, _ = _world(repo, tmp_path / "b")
    w2.worker_id = "worker-2"
    rep = w2.start()                                                              # new process: foreign lease -> recovered
    assert [(a.run_id, a.action) for a in rep.actions] == [(dead.run_id, "requeued")]
    clock.advance(Limits().backoff_ms(1))
    w2.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW
    runs = repo.list_runs(t.task_id)
    assert [r.state.value for r in runs] == ["abandoned", "succeeded"] and runs[1].attempt == 2
    assert repo.verify_chain().ok
    # the abandoned attempt was charged at worst case, and nothing is left reserved
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve"} == {e.reserve_ref for e in ledger if e.kind.value == "release"}


# ------------------------------------------------------------------ E2E-03: failure path


def test_permanent_failure_fails_the_task_and_the_objective_fail_fast(repo, tmp_path):
    worker, _ = _world(repo, tmp_path, executor_script=lambda r: "permanent_failure")
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
    child = repo.create_task(_analysis_contract(), PLANNER, root.task_id).task
    root = _plan_and_approve(repo, root, needs_human=False)
    child = _plan_and_approve(repo, child, needs_human=True)
    for _ in range(4):
        worker.tick()
    assert repo.get_task(child.task_id).state is TaskState.FAILED
    assert repo.get_task(root.task_id).state is TaskState.FAILED
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve"} == {e.reserve_ref for e in ledger if e.kind.value == "release"}
    assert repo.verify_chain().ok
