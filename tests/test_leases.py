"""Lease / heartbeat / recovery. Uses FakeClock only: no sleeping anywhere."""
from __future__ import annotations

import pytest

from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import (
    ApprovalRequirement,
    ApprovalType,
    ErrorRecord,
    ResourceLimits,
    SideEffectClass,
)
from airos.domain.states import BlockReason, Event, RunState, TaskState
from airos.errors import GuardFailed, LeaseLost, LimitExceeded
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import EXECUTOR, HUMAN, approve_fully, make_contract, to_running

LEASE = Limits().lease_ttl_ms


def _side_effectful_contract(**kw):
    return make_contract(
        side_effect_class=SideEffectClass.EXTERNAL_WRITE,
        approval_requirements=[
            ApprovalRequirement(type=ApprovalType.EXECUTE, reason="x"),
            ApprovalRequirement(type=ApprovalType.COST, reason="x"),
            ApprovalRequirement(type=ApprovalType.EXTERNAL_HANDOFF, reason="x"),
        ],
        **kw,
    )


def test_heartbeat_extends_lease_and_expiry_is_exact(repo, clock):
    t = repo.create_task(make_contract(), HUMAN).task
    t, run_id = to_running(repo, t)
    clock.advance(LEASE - 1)
    assert repo.heartbeat(run_id, "w1") is True          # still valid, extended to now+60s
    clock.advance(LEASE - 1)
    assert repo.heartbeat(run_id, "w1") is True
    clock.advance(LEASE)                                  # exactly at expiry -> lost
    assert repo.heartbeat(run_id, "w1") is False
    assert repo.heartbeat(run_id, "someone-else") is False


def test_expired_lease_recovery_requeues_with_backoff_then_reruns(repo, clock):
    t = repo.create_task(make_contract(), HUMAN).task
    t, run_id = to_running(repo, t)
    clock.advance(LEASE)
    with pytest.raises(LeaseLost):  # worker must discard its late result
        repo.complete_run(run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    report = repo.recover_leases()
    assert [(a.run_id, a.action) for a in report.actions] == [(run_id, "requeued")]
    t = repo.get_task(t.task_id)
    assert t.state is TaskState.QUEUED and t.not_before_ms == clock.now_ms() + Limits().backoff_ms(1)
    assert repo.list_runs(t.task_id)[0].state is RunState.ABANDONED
    assert t.error_history[-1].error_class == "crash_recovery"
    # not_before gate: cannot restart before backoff elapsed
    with pytest.raises(GuardFailed, match="not_before"):
        repo.start_run(t.task_id, "w2", EXECUTOR.id, t.version, GuardContext(budget_reserved=True))
    clock.advance(Limits().backoff_ms(1))
    run2 = repo.start_run(t.task_id, "w2", EXECUTOR.id, t.version, GuardContext(budget_reserved=True))
    assert run2.attempt == 2 and run2.run_id != run_id
    assert repo.recover_leases().actions == []           # nothing else to recover; idempotent
    assert repo.verify_chain().ok


def test_recovery_is_deterministic_and_idempotent(repo, clock):
    ids = []
    for _ in range(2):
        t = repo.create_task(make_contract(), HUMAN).task
        _, rid = to_running(repo, t, worker="w1")
        ids.append(rid)
        clock.advance(1)  # distinct start times; ties would be broken by run_id (also deterministic)
    clock.advance(LEASE)
    first = repo.recover_leases()
    assert [a.run_id for a in first.actions] == ids   # ordered by start time then id
    assert repo.recover_leases().actions == []


def test_recovery_by_foreign_owner_on_startup_even_if_lease_unexpired(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    _, rid = to_running(repo, t, worker="dead-worker")
    assert repo.recover_leases("dead-worker").actions == []                     # owner is me: untouched
    assert [a.action for a in repo.recover_leases("new-process").actions] == ["requeued"]


def test_side_effectful_task_is_never_auto_rerun(repo, clock):
    t = repo.create_task(_side_effectful_contract(), HUMAN).task
    t, rid = to_running(repo, t)
    clock.advance(LEASE)
    rep = repo.recover_leases()
    assert [a.action for a in rep.actions] == ["blocked"]
    t = repo.get_task(t.task_id)
    assert t.state is TaskState.BLOCKED and t.blocked_reason is BlockReason.CRASH_RECOVERY_DECISION
    assert len(repo.list_runs(t.task_id)) == 1                                   # no second run was created
    # the system cannot resume it; only a human can
    with pytest.raises(GuardFailed, match="human"):
        repo.transition(t.task_id, Event.UNBLOCK, SYSTEM, t.version)
    clock.advance(10 * LEASE)
    assert repo.recover_leases().actions == []
    assert repo.get_task(t.task_id).state is TaskState.BLOCKED
    assert repo.transition(t.task_id, Event.UNBLOCK, HUMAN, t.version).state is TaskState.QUEUED


def test_side_effectful_retry_event_is_rejected_by_guard_even_for_system(repo):
    t = repo.create_task(_side_effectful_contract(), HUMAN).task
    t, _ = to_running(repo, t)
    with pytest.raises(GuardFailed, match="never auto-retried"):
        repo.transition(t.task_id, Event.RETRY, SYSTEM, t.version, GuardContext(error_retryable=True))


def test_side_effectful_retryable_failure_goes_to_failed_not_queued(repo):
    t = repo.create_task(_side_effectful_contract(), HUMAN).task
    t, rid = to_running(repo, t)
    out = repo.complete_run(rid, "w1", RunState.FAILED, retryable=True,
                            error=ErrorRecord(ts_ms=1, attempt=1, error_class="transient", retryable=True, detail="x"))
    assert out.state is TaskState.FAILED


def test_retry_limit_then_failed(repo, clock):
    t = repo.create_task(make_contract(resource_limits=ResourceLimits(max_usd_micro=1_000_000, max_retries=1)), HUMAN).task
    t = approve_fully(repo, t)
    attempts = 0
    while True:
        t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version) if t.state is TaskState.APPROVED else t
        clock.advance(10_000_000)
        run = repo.start_run(t.task_id, "w", EXECUTOR.id, t.version, GuardContext(budget_reserved=True))
        attempts += 1
        t = repo.complete_run(run.run_id, "w", RunState.FAILED, retryable=True)
        if t.state is TaskState.FAILED:
            break
    assert attempts == 2  # 1 + max_retries
    assert [r.state for r in repo.list_runs(t.task_id)] == [RunState.FAILED, RunState.FAILED]


def test_non_retryable_failure_and_timeout_paths(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t, rid = to_running(repo, t)
    assert repo.complete_run(rid, "w1", RunState.FAILED, retryable=False).state is TaskState.FAILED
    t2 = repo.create_task(make_contract(resource_limits=ResourceLimits(max_usd_micro=1_000_000, max_retries=0)), HUMAN).task
    t2, rid2 = to_running(repo, t2)
    assert repo.complete_run(rid2, "w1", RunState.TIMED_OUT, retryable=True).state is TaskState.TIMED_OUT


def test_success_requires_verifier_pass(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t, rid = to_running(repo, t)
    with pytest.raises(GuardFailed, match="verifier"):
        repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=False)
    assert repo.get_task(t.task_id).state is TaskState.RUNNING  # rolled back; run still active
    assert repo.list_runs(t.task_id)[0].state is RunState.RUNNING


def test_only_one_active_run_per_task_and_concurrency_cap(open_repo):
    repo = open_repo(limits=Limits(max_concurrent_runs=1))
    a = repo.create_task(make_contract(), HUMAN).task
    b = repo.create_task(make_contract(), HUMAN).task
    to_running(repo, a)
    b = approve_fully(repo, b)
    b = repo.transition(b.task_id, Event.QUEUE, SYSTEM, b.version)
    with pytest.raises(LimitExceeded):
        repo.start_run(b.task_id, "w2", EXECUTOR.id, b.version, GuardContext(budget_reserved=True))


def test_start_requires_budget_reservation_and_matching_agent_role(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = approve_fully(repo, t)
    t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    with pytest.raises(GuardFailed, match="budget"):
        repo.start_run(t.task_id, "w", EXECUTOR.id, t.version, GuardContext())
    from airos.errors import NotAuthorized
    with pytest.raises(NotAuthorized):
        repo.start_run(t.task_id, "w", "planner-1", t.version, GuardContext(budget_reserved=True))


def test_timestamps_come_from_injected_clock(repo, clock):
    t = repo.create_task(make_contract(), HUMAN).task
    assert t.created_at_ms == clock.now_ms()
    assert all(e.ts_ms == clock.now_ms() for e in repo.iter_events())
