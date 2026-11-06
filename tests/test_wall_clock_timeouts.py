"""Hard per-run wall-clock timeout, enforced by the control plane/worker independent of the adapter. Fake clock only."""
from __future__ import annotations

import threading

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.limits import Limits
from airos.domain.schemas import ExpectedOutput, ResourceLimits, Role, SideEffectClass
from airos.domain.states import RunState, TaskState
from airos.errors import GuardFailed, LeaseLost, NotAuthorized
from airos.persistence import budget_sql
from tests.conftest import AGENT_ACTORS, EXECUTOR, HUMAN, approve_fully, make_contract

WALL_S = 300
MS = 1000
USD = 1_000_000


def _contract(**over):
    return make_contract(resource_limits=ResourceLimits(max_usd_micro=USD, max_retries=over.pop("max_retries", 2), wall_clock_s=WALL_S),
                         expected_output=[ExpectedOutput(logical_name="o.txt", media_type="text/plain", artifact_class="work_product")],
                         **over)


def _running(repo, **over):
    t = approve_fully(repo, repo.create_task(_contract(**over), HUMAN).task)
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "w1", EXECUTOR.id, t.version)
    return repo.get_task(t.task_id), run


def test_exact_boundary_timeout_minus_1s_ok_timeout_plus_1s_expired(repo, clock):
    t, run = _running(repo)
    clock.advance(WALL_S * MS - 1 * MS)                                   # timeout - 1s
    assert repo.time_out_expired_runs() == []
    with pytest.raises(GuardFailed, match="not reached"):
        repo.time_out_run(run.run_id)
    assert repo.list_runs(t.task_id)[0].state is RunState.RUNNING
    clock.advance(2 * MS)                                                 # timeout + 1s
    assert repo.time_out_expired_runs() == [run.run_id]
    assert repo.list_runs(t.task_id)[0].state is RunState.TIMED_OUT


def test_the_deadline_instant_itself_counts_as_expired(repo, clock):
    t, run = _running(repo)
    clock.advance(WALL_S * MS - 1)
    assert repo.time_out_expired_runs() == []
    clock.advance(1)                                                      # elapsed == wall_clock_s exactly
    assert repo.time_out_expired_runs() == [run.run_id]


def _keep_alive_until(repo, clock, run, elapsed_ms):
    """Advance the fake clock to `elapsed_ms` after the run started, heartbeating so the LEASE never expires."""
    start = clock.now_ms()
    while clock.now_ms() - start < elapsed_ms:
        clock.advance(min(30 * MS, elapsed_ms - (clock.now_ms() - start)))
        assert repo.heartbeat(run.run_id, "w1")


def test_result_delivered_1s_before_the_deadline_is_accepted(repo, clock):
    t, run = _running(repo)
    _keep_alive_until(repo, clock, run, WALL_S * MS - MS)                 # timeout - 1s
    out = repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    assert out.state is TaskState.REVIEW and repo.list_runs(t.task_id)[0].state is RunState.SUCCEEDED


def test_result_delivered_1s_after_the_deadline_is_discarded_even_with_a_valid_lease(repo, clock):
    t, run = _running(repo)
    _keep_alive_until(repo, clock, run, WALL_S * MS + MS)                 # timeout + 1s, lease heartbeated throughout
    out = repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    assert out.state is TaskState.QUEUED                                  # discarded -> timeout, one timeout retry
    assert repo.list_runs(t.task_id)[0].state is RunState.TIMED_OUT


def test_timeout_settles_at_worst_case_then_retries_once(repo, clock):
    t, run = _running(repo)
    clock.advance(WALL_S * MS + MS)
    repo.time_out_run(run.run_id)
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.QUEUED and cur.timeout_retries == 1 and cur.error_history[-1].error_class == "timeout"
    assert cur.not_before_ms == clock.now_ms() + Limits().backoff_ms(1)
    for bid in (f"task:{t.task_id}", f"root:{t.root_task_id}", budget_sql.global_budget_id(clock.now_ms())):
        # attempt 1 settled at its full reservation; attempt 2 holds a fresh open one
        assert repo.budget_status(bid).used_usd_micro == 2 * USD
    settle = [e for e in repo.list_ledger() if e.kind.value == "settle" and e.task_id == t.task_id]
    assert settle and all(e.usd_micro == USD for e in settle)
    assert budget_sql.open_task_reserve(repo._conn(), t.task_id, 2) is not None


def test_second_timeout_is_terminal(repo, clock):
    t, run = _running(repo)
    clock.advance(WALL_S * MS + MS)
    repo.time_out_run(run.run_id)
    clock.advance(Limits().backoff_ms(1))
    run2 = repo.start_run(t.task_id, "w1", EXECUTOR.id, repo.get_task(t.task_id).version)
    clock.advance(WALL_S * MS)
    repo.time_out_run(run2.run_id)
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.TIMED_OUT and cur.attempt_count == 2 and cur.timeout_retries == 1
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve"} == {e.reserve_ref for e in ledger if e.kind.value == "release"}
    assert [r.state for r in repo.list_runs(t.task_id)] == [RunState.TIMED_OUT, RunState.TIMED_OUT]


def test_retry_rules_still_apply_no_retries_means_terminal_on_first_timeout(repo, clock):
    t, run = _running(repo, max_retries=0)
    clock.advance(WALL_S * MS + MS)
    repo.time_out_run(run.run_id)
    assert repo.get_task(t.task_id).state is TaskState.TIMED_OUT


def test_side_effectful_task_is_not_retried_after_a_timeout(repo, clock):
    from airos.domain.schemas import ApprovalRequirement, ApprovalType

    reqs = [ApprovalRequirement(type=x, reason="x") for x in (ApprovalType.EXECUTE, ApprovalType.COST, ApprovalType.EXTERNAL_HANDOFF)]
    t, run = _running(repo, side_effect_class=SideEffectClass.EXTERNAL_WRITE, approval_requirements=reqs)
    clock.advance(WALL_S * MS + MS)
    repo.time_out_run(run.run_id)
    assert repo.get_task(t.task_id).state is TaskState.TIMED_OUT


def test_sweep_times_out_runs_of_a_dead_worker_on_the_next_tick(repo, clock, tmp_path):
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], all_mock_adapters(), {})
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w2")
    t, run = _running(repo)
    clock.advance(WALL_S * MS + MS)
    w.tick()
    assert repo.list_runs(t.task_id)[0].state is RunState.TIMED_OUT


def test_only_the_system_may_time_a_run_out(repo, clock):
    t, run = _running(repo)
    clock.advance(WALL_S * MS + MS)
    for a in (*AGENT_ACTORS, HUMAN):
        with pytest.raises(NotAuthorized):
            repo.time_out_run(run.run_id, actor=a)
    assert repo.list_runs(t.task_id)[0].state is RunState.RUNNING


def test_timed_out_run_loses_its_lease(repo, clock):
    t, run = _running(repo)
    clock.advance(WALL_S * MS + MS)
    repo.time_out_run(run.run_id)
    assert repo.heartbeat(run.run_id, "w1") is False
    with pytest.raises(LeaseLost):
        repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)


# ------------------------------------------------------------------ the worker enforces it on a hung adapter


def _worker(repo, tmp_path, adapter):
    adapters = all_mock_adapters()
    adapters[Role.EXECUTOR] = adapter
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], adapters, {})
    return Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w1")


def test_hung_adapter_is_cut_off_by_the_worker_without_sleeping(repo, clock, tmp_path):
    release = threading.Event()

    class Hanging(MockAdapter):
        def execute(self, request, workdir):
            release.wait(30)                       # a hung adapter; released by the test so no thread leaks
            return super().execute(request, workdir)

    w = _worker(repo, tmp_path, Hanging(Role.EXECUTOR))
    w._join = lambda th, timeout_s, run_id: clock.advance(timeout_s * MS)   # fake clock: "waiting timeout_s seconds" costs no time
    t = approve_fully(repo, repo.create_task(_contract(), HUMAN).task)
    w.tick()
    cur = repo.get_task(t.task_id)
    release.set()
    assert cur.state is TaskState.QUEUED and cur.timeout_retries == 1
    assert repo.list_runs(t.task_id)[0].state is RunState.TIMED_OUT
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 2 * USD    # worst case + the new open reservation


def _slow_adapter(repo, clock, elapsed_ms):
    class Slow(MockAdapter):
        def execute(self, request, workdir):
            start = clock.now_ms()
            while clock.now_ms() - start < elapsed_ms:      # models the worker's background heartbeater
                clock.advance(min(30 * MS, elapsed_ms - (clock.now_ms() - start)))
                repo.heartbeat(request.run_id, "w1")
            return super().execute(request, workdir)

    return Slow(Role.EXECUTOR)


def test_adapter_that_returns_1s_inside_the_limit_is_accepted(repo, clock, tmp_path):
    w = _worker(repo, tmp_path, _slow_adapter(repo, clock, WALL_S * MS - MS))
    t = approve_fully(repo, repo.create_task(_contract(), HUMAN).task)
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW


def test_adapter_that_returns_1s_after_the_limit_is_discarded(repo, clock, tmp_path):
    w = _worker(repo, tmp_path, _slow_adapter(repo, clock, WALL_S * MS + MS))
    t = approve_fully(repo, repo.create_task(_contract(), HUMAN).task)
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.QUEUED
    assert repo.list_runs(t.task_id)[0].state is RunState.TIMED_OUT
    assert repo._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0   # its output was never registered
