"""Agent registry and routing: fail closed, audited, mock adapters only."""
from __future__ import annotations

import pytest

from airos.agents.adapters import all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.guards import GuardContext
from airos.domain.schemas import Role
from airos.domain.states import BlockReason, Event, TaskState
from airos.errors import AdapterUnavailable
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import HUMAN, TEST_PRICES, approve_fully, make_contract

FAKE_KEY = "fake-" + "credential-" + "value-98765"
ROLES = [r.value for r in Role]


def _registry(specs=None, environ=None):
    specs = specs if specs is not None else [AgentSpec(role=r) for r in ROLES]
    return AgentRegistry(specs, all_mock_adapters(), environ or {}, TEST_PRICES)


def test_all_five_roles_resolve_to_mock_adapters():
    reg = _registry()
    for r in ROLES:
        a = reg.resolve(r)
        assert a.is_mock and a.role.value == r


def test_unknown_role_fails_closed():
    reg = _registry()
    for bad in ("wizard", "", "Executor", "executor ", "../executor"):
        with pytest.raises(AdapterUnavailable, match="unknown role"):
            reg.resolve(bad)
    with pytest.raises(AdapterUnavailable):        # a known Role with no spec registered at all
        _registry([AgentSpec(role="planner")]).resolve("executor")


def test_disabled_adapter_fails_closed():
    reg = _registry([AgentSpec(role=r, enabled=(r != "executor")) for r in ROLES])
    with pytest.raises(AdapterUnavailable, match="disabled"):
        reg.resolve("executor")
    assert reg.resolve("planner").is_mock


@pytest.mark.parametrize("environ", [{}, {"EXEC_KEY": ""}, {"OTHER": FAKE_KEY}])
def test_real_adapter_without_credentials_fails_closed(environ):
    reg = _registry([AgentSpec(role="executor", kind="real", credential_env="EXEC_KEY")], environ)
    with pytest.raises(AdapterUnavailable, match="no configured credentials"):
        reg.resolve("executor")
    reg2 = _registry([AgentSpec(role="executor", kind="real", credential_env=None)], environ)
    with pytest.raises(AdapterUnavailable, match="no configured credentials"):
        reg2.resolve("executor")


def test_real_adapter_with_credentials_still_fails_closed_because_none_is_implemented_in_m2():
    reg = _registry([AgentSpec(role="executor", kind="real", credential_env="EXEC_KEY", model="unit-model")], {"EXEC_KEY": FAKE_KEY})
    with pytest.raises(AdapterUnavailable, match="no real adapter implementation"):
        reg.resolve("executor")
    assert reg.real_roles() == {"executor"}


def test_routing_failure_is_audited_and_never_leaks_the_credential(repo):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    reg = _registry([AgentSpec(role="executor", kind="real", credential_env="EXEC_KEY", model="unit-model")], {"EXEC_KEY": FAKE_KEY})
    with pytest.raises(AdapterUnavailable):
        reg.route(repo, t)
    ev = [e for e in repo.iter_events() if e.event_type == "agent.route_failed"]
    assert len(ev) == 1 and ev[0].task_id == t.task_id and ev[0].payload["role"] == "executor"
    assert "no real adapter implementation" in ev[0].payload["reason"]
    assert all(FAKE_KEY not in e.model_dump_json() for e in repo.iter_events())
    assert repo.verify_chain().ok


def test_worker_blocks_a_task_whose_adapter_is_unavailable_then_resumes_when_fixed(repo, tmp_path):
    store = ArtifactStore(tmp_path / "cas", tmp_path / "eval")
    svc = ArtifactService(repo, store)
    disabled = _registry([AgentSpec(role=r, enabled=(r != "executor")) for r in ROLES])
    w = Worker(repo, disabled, FreshDirRunner(tmp_path / "ws"), svc, "w1")
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    rep = w.tick()
    assert rep.started == [] and rep.blocked_unroutable == [t.task_id]
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.ADAPTER_UNAVAILABLE
    assert repo.list_runs(t.task_id) == []                                      # nothing ran
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 0          # and the reservation was released
    assert [e.payload["role"] for e in repo.iter_events() if e.event_type == "agent.route_failed"] == ["executor"]
    # human re-enables the adapter; the system may resume ONLY once the blocker is cleared
    w2 = Worker(repo, _registry(), FreshDirRunner(tmp_path / "ws2"), svc, "w2")
    with pytest.raises(Exception, match="adapter still unavailable"):
        repo.enqueue(cur.task_id, Event.UNBLOCK, SYSTEM, cur.version, GuardContext())
    resumed = repo.enqueue(cur.task_id, Event.UNBLOCK, SYSTEM, cur.version, GuardContext(blocker_cleared=True))
    assert resumed.state is TaskState.QUEUED
    w2.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW


def test_ttl_sweeps_time_out_stale_approvals_reviews_and_blocked_tasks(repo, tmp_path, clock):
    from airos.domain.guards import GuardContext as Ctx
    from airos.domain.limits import Limits
    from tests.conftest import PLANNER, to_running

    store = ArtifactStore(tmp_path / "cas", tmp_path / "eval")
    w = Worker(repo, _registry(), FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, store), "w1")
    # awaiting approval (no decision) -> expire after the approval TTL
    a = repo.create_task(make_contract(), HUMAN).task
    a = repo.transition(a.task_id, Event.PLAN, PLANNER, a.version, Ctx(plan_ok=True))
    a = repo.transition(a.task_id, Event.REQUIRE_APPROVAL, SYSTEM, a.version)
    from airos.domain.schemas import ApprovalType as AT
    for r in a.contract.approval_requirements:
        repo.request_approval(a.task_id, AT(r.type), SYSTEM)
    # review that nobody looks at
    r_task, run_id = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    from airos.domain.states import RunState
    repo.complete_run(run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    # blocked (budget) task nobody resolves
    b = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.set_budget(f"root:{b.root_task_id}", "root", b.root_task_id, 0, 0, HUMAN)
    b = repo.queue_task(b.task_id, b.version)
    assert b.state is TaskState.BLOCKED
    assert w.sweep_timeouts() == []                                              # nothing is stale yet
    clock.advance(Limits().approval_ttl_ms)
    moved = w.sweep_timeouts()
    assert set(moved) == {a.task_id, r_task.task_id}                              # approval TTL == review TTL == 72h
    assert repo.get_task(b.task_id).state is TaskState.BLOCKED
    clock.advance(Limits().blocked_ttl_ms)
    assert w.sweep_timeouts() == [b.task_id]
    assert {repo.get_task(x).state for x in (a.task_id, r_task.task_id, b.task_id)} == {TaskState.TIMED_OUT}
    assert repo.verify_chain().ok
