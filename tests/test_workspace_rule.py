"""Workspace rule: workspace_write tasks are re-runnable ONLY if the runner provably gives every run a fresh directory;
otherwise they are treated as side-effectful (never auto-rerun). Mock runners only."""
from __future__ import annotations

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner, SharedDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ExpectedOutput, Role, SideEffectClass
from airos.domain.states import BlockReason, Event, TaskState
from airos.errors import GuardFailed
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import HUMAN, approve_fully, make_contract

ROLES = [r.value for r in Role]


class LyingRunner:
    """Claims freshness but reuses one directory. The worker's black-box proof must not believe the claim."""

    guarantees_fresh_run_dirs = True

    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def prove_fresh_run_dirs(self) -> bool:
        return True

    def prepare(self, task_id, run_id):
        d = self.root / "same"
        d.mkdir(exist_ok=True)
        return d


def _worker(repo, runner, tmp_path, script=None):
    adapters = all_mock_adapters()
    if script is not None:
        adapters[Role.EXECUTOR] = MockAdapter(Role.EXECUTOR, script=script)
    reg = AgentRegistry([AgentSpec(role=r) for r in ROLES], adapters, {})
    return Worker(repo, reg, runner, ArtifactService(repo, ArtifactStore(tmp_path / "cas", tmp_path / "eval")), "worker-1")


def _task(repo, cls):
    c = make_contract(side_effect_class=cls,
                      expected_output=[ExpectedOutput(logical_name="o.txt", media_type="text/plain",
                                                      artifact_class="work_product")])
    return approve_fully(repo, repo.create_task(c, HUMAN).task)


def test_fresh_dir_runner_is_proven_and_workspace_write_is_requeued_after_a_crash(repo, tmp_path, clock):
    w = _worker(repo, FreshDirRunner(tmp_path / "ws"), tmp_path)
    assert repo._workspace_fresh_guaranteed is True
    t = _task(repo, SideEffectClass.WORKSPACE_WRITE)
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "dead-worker", "executor-1", t.version)
    first_dir = w.runner.prepare(t.task_id, run.run_id)
    (first_dir / "partial.tmp").write_text("leftover from the crashed run")
    clock.advance(Limits().lease_ttl_ms)
    rep = w.start()
    assert [a.action for a in rep.actions] == ["requeued"]
    clock.advance(Limits().backoff_ms(1))
    w.tick()
    done = repo.get_task(t.task_id)
    assert done.state is TaskState.REVIEW and done.attempt_count == 2
    second = repo.list_runs(t.task_id)[1]
    second_dir = w.runner.root / second.run_id
    assert second_dir != first_dir and not (second_dir / "partial.tmp").exists()   # the rerun saw a clean directory


def test_shared_dir_runner_cannot_prove_freshness_so_workspace_write_is_side_effectful(repo, tmp_path, clock):
    w = _worker(repo, SharedDirRunner(tmp_path / "ws"), tmp_path)
    assert repo._workspace_fresh_guaranteed is False
    t = _task(repo, SideEffectClass.WORKSPACE_WRITE)
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "dead-worker", "executor-1", t.version)
    clock.advance(Limits().lease_ttl_ms)
    rep = w.start()
    assert [a.action for a in rep.actions] == ["blocked"]                         # never auto-rerun
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.CRASH_RECOVERY_DECISION
    assert len(repo.list_runs(t.task_id)) == 1 and run.run_id
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.BLOCKED                    # the worker will not touch it
    with pytest.raises(GuardFailed, match="human"):
        repo.enqueue(cur.task_id, Event.UNBLOCK, SYSTEM, cur.version, GuardContext(blocker_cleared=True))
    assert repo.enqueue(cur.task_id, Event.UNBLOCK, HUMAN, cur.version).state is TaskState.QUEUED  # only a human decides


def test_a_runner_that_lies_about_freshness_is_not_believed(repo, tmp_path):
    _worker(repo, LyingRunner(tmp_path / "ws"), tmp_path)
    assert repo._workspace_fresh_guaranteed is False


def test_workspace_write_retryable_failure_is_not_retried_without_the_guarantee(repo, tmp_path):
    _worker(repo, SharedDirRunner(tmp_path / "ws"), tmp_path, script=lambda r: "transient_failure")
    t = _task(repo, SideEffectClass.WORKSPACE_WRITE)
    w = _worker(repo, SharedDirRunner(tmp_path / "ws2"), tmp_path, script=lambda r: "transient_failure")
    w.tick()
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.FAILED and repo.get_task(t.task_id).attempt_count == 1


def test_workspace_write_retryable_failure_IS_retried_with_the_guarantee(repo, tmp_path, clock):
    w = _worker(repo, FreshDirRunner(tmp_path / "ws"), tmp_path,
                script=lambda r: "transient_failure" if r.attempt == 1 else "success")
    t = _task(repo, SideEffectClass.WORKSPACE_WRITE)
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.QUEUED
    clock.advance(Limits().backoff_ms(1))
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW and repo.get_task(t.task_id).attempt_count == 2


@pytest.mark.parametrize("cls", [SideEffectClass.EXTERNAL_WRITE, SideEffectClass.DESTRUCTIVE])
def test_external_effects_are_never_rerun_even_with_a_proven_fresh_runner(repo, tmp_path, clock, cls):
    from airos.domain.schemas import ApprovalRequirement, ApprovalType

    w = _worker(repo, FreshDirRunner(tmp_path / "ws"), tmp_path)
    assert repo._workspace_fresh_guaranteed is True
    reqs = [ApprovalRequirement(type=t, reason="x") for t in (ApprovalType.EXECUTE, ApprovalType.COST,
                                                              ApprovalType.EXTERNAL_HANDOFF, ApprovalType.DESTRUCTIVE)]
    t = approve_fully(repo, repo.create_task(make_contract(side_effect_class=cls, approval_requirements=reqs), HUMAN).task)
    t = repo.queue_task(t.task_id, t.version)
    repo.start_run(t.task_id, "dead", "executor-1", t.version)
    clock.advance(Limits().lease_ttl_ms)
    assert [a.action for a in w.start().actions] == ["blocked"]


def test_none_side_effect_class_is_always_rerunnable(repo, tmp_path, clock):
    w = _worker(repo, SharedDirRunner(tmp_path / "ws"), tmp_path)           # even without a fresh-dir guarantee
    t = _task(repo, SideEffectClass.NONE)
    t = repo.queue_task(t.task_id, t.version)
    repo.start_run(t.task_id, "dead", "executor-1", t.version)
    clock.advance(Limits().lease_ttl_ms)
    assert [a.action for a in w.start().actions] == ["requeued"]
