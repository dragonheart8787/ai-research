"""A human approval grant is valid for 7 days (PROPOSAL T10/T11): valid strictly before decided_at + 7d, invalid at
and after it. Fake clock only."""
from __future__ import annotations

import pytest

from airos.agents.adapters import all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ApprovalStatus, ApprovalType, Role
from airos.domain.states import Event, TaskState
from airos.errors import ApprovalExpired, GuardFailed
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import HUMAN, PLANNER, approve_fully, make_contract

TTL = Limits().grant_ttl_ms
assert TTL == 7 * 24 * 3600 * 1000


def test_boundary_7d_minus_1ms_is_valid_7d_is_expired(repo, clock):
    a = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    b = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(TTL - 1)                                              # still valid, 1 ms before expiry
    assert repo.queue_task(a.task_id, a.version).state is TaskState.QUEUED
    clock.advance(1)                                                    # exactly 7 days after the grant
    with pytest.raises(GuardFailed, match="approvals are missing or stale"):
        repo.queue_task(b.task_id, b.version)
    assert repo.get_task(b.task_id).state is TaskState.APPROVED          # refused atomically; nothing reserved
    assert [e for e in repo.list_ledger() if e.task_id == b.task_id] == []


def test_expired_grants_force_reapproval_and_a_fresh_grant_works(repo, clock):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(TTL)
    t = repo.transition(t.task_id, Event.REAPPROVE, SYSTEM, t.version, reason="grant expired")
    assert t.state is TaskState.AWAITING_APPROVAL
    old = repo.list_approvals(t.task_id)
    assert {a.status for a in old} == {ApprovalStatus.EXPIRED}
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)       # expired grants do not count
    for r in t.contract.approval_requirements:                           # a new request is possible for the SAME contract version
        ap = repo.request_approval(t.task_id, ApprovalType(r.type), SYSTEM)
        assert ap.status is ApprovalStatus.PENDING and ap.task_version == t.contract_version
        repo.decide_approval(ap.approval_id, True, HUMAN, "re-approved")
    t = repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)
    assert repo.queue_task(t.task_id, t.version).state is TaskState.QUEUED


def test_reapprove_is_refused_while_the_grants_are_still_valid(repo, clock):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(TTL - 1)
    with pytest.raises(GuardFailed, match="still valid"):
        repo.transition(t.task_id, Event.REAPPROVE, SYSTEM, t.version)


def test_a_grant_in_awaiting_approval_also_expires(repo, clock):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    for r in t.contract.approval_requirements:
        repo.decide_approval(repo.request_approval(t.task_id, ApprovalType(r.type), SYSTEM).approval_id, True, HUMAN)
    clock.advance(TTL - 1)
    # still valid: APPROVE would succeed; prove it on a twin, then show the expired case on this one
    clock.advance(1)
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)


def test_worker_sweep_moves_expired_approved_tasks_back_to_awaiting(repo, clock, tmp_path):
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], all_mock_adapters(), {})
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w")
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(TTL - 1)
    w.sweep_timeouts()
    assert repo.get_task(t.task_id).state is TaskState.APPROVED
    clock.advance(1)
    w.sweep_timeouts()
    assert repo.get_task(t.task_id).state is TaskState.AWAITING_APPROVAL


def test_a_task_already_queued_or_running_is_not_affected_by_later_expiry(repo, clock):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "w1", "executor-1", t.version)
    clock.advance(TTL + 1000)
    repo.heartbeat(run.run_id, "w1")
    assert repo.get_task(t.task_id).state is TaskState.RUNNING          # authorisation was consumed when it was queued


def test_budget_increase_grant_expires_before_it_is_applied(repo, clock):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    bid = f"root:{t.root_task_id}"
    early = repo.request_budget_increase(t.task_id, bid, 10, 10, PLANNER)
    late = repo.request_budget_increase(t.task_id, f"global:{'x'}", 5, 5, PLANNER)
    for a in (early, late):
        repo.decide_approval(a.approval_id, True, HUMAN)
    clock.advance(TTL - 1)
    applied = repo.grant_budget(early.approval_id)                       # 1 ms before expiry: applied
    assert applied.usd_micro == 10
    clock.advance(1)
    with pytest.raises(ApprovalExpired):                                 # exactly at expiry: refused ...
        repo.grant_budget(late.approval_id)
    assert repo.get_approval(late.approval_id).status is ApprovalStatus.EXPIRED          # ... and recorded
    assert any(e.event_type == "approval.expired" and e.payload["approval_id"] == late.approval_id for e in repo.iter_events())
    assert [e for e in repo.list_ledger() if e.approval_id == late.approval_id] == []
    # an already-applied grant stays applied, and replay is still the same single entry
    assert repo.grant_budget(early.approval_id).entry_id == applied.entry_id
    assert len([e for e in repo.list_ledger() if e.kind.value == "grant"]) == 1


def test_expired_grants_cannot_be_revived_by_raw_status_edits_via_the_service_path(repo, clock):
    """The service re-checks the clock on every use; a row forced back to 'granted' is still expired by age."""
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(TTL)
    repo._conn().execute("UPDATE approvals SET status='granted' WHERE task_id=?", (t.task_id,))
    with pytest.raises(GuardFailed, match="approvals"):
        repo.queue_task(t.task_id, t.version)
