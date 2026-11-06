from __future__ import annotations

import sqlite3

import pytest

from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ApprovalStatus, ApprovalType, ResourceLimits, TaskState, contract_sha256
from airos.domain.states import Event
from airos.errors import (
    ApprovalExpired,
    GuardFailed,
    IllegalTransition,
    NotAuthorized,
    StaleApproval,
    VersionConflict,
)
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import DIRECTOR, EXECUTOR, HUMAN, PLANNER, approve_fully, make_contract


def _awaiting(repo, **kw):
    t = repo.create_task(make_contract(**kw), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    return repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)


def test_approval_records_task_version_and_contract_hash(repo):
    t = _awaiting(repo)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    assert ap.task_id == t.task_id and ap.task_version == t.contract_version == 1
    assert ap.contract_sha256 == contract_sha256(t.contract) == t.contract_sha256
    assert ap.status is ApprovalStatus.PENDING
    assert repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM).approval_id == ap.approval_id  # idempotent


def test_contract_change_invalidates_existing_approvals_and_blocks_stale_approve(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = approve_fully(repo, t)
    assert t.state is TaskState.APPROVED
    old = repo.list_approvals(t.task_id)
    assert {a.status for a in old} == {ApprovalStatus.GRANTED}

    changed = make_contract(idempotency_key=t.idempotency_key, research_objective="changed objective",
                            resource_limits=t.contract.resource_limits)
    t2 = repo.amend_task(t.task_id, changed, t.version, HUMAN)
    assert t2.contract_version == 2 and t2.version > t.version and t2.contract_sha256 != t.contract_sha256
    assert t2.state is TaskState.AWAITING_APPROVAL                                  # approved -> back to awaiting
    assert {a.status for a in repo.list_approvals(t.task_id)} == {ApprovalStatus.INVALIDATED}
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(t.task_id, Event.APPROVE, HUMAN, t2.version)               # stale grants do not count

    # fresh approvals bound to version 2 do
    for req in t2.contract.approval_requirements:
        ap = repo.request_approval(t.task_id, req.type, SYSTEM)
        assert ap.task_version == 2 and ap.contract_sha256 == t2.contract_sha256
        repo.decide_approval(ap.approval_id, True, HUMAN)
    assert repo.transition(t.task_id, Event.APPROVE, HUMAN, repo.get_task(t.task_id).version).state is TaskState.APPROVED
    assert any(e.event_type == "task.amended" and e.payload["approvals_invalidated"] == 2 for e in repo.iter_events())


def test_budget_change_counts_as_contract_change(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = approve_fully(repo, t)
    bigger = make_contract(idempotency_key=t.idempotency_key,
                           resource_limits=ResourceLimits(max_usd_micro=2_000_000, max_retries=2))
    t2 = repo.amend_task(t.task_id, bigger, t.version, HUMAN)
    assert t2.contract_version == 2 and t2.state is TaskState.AWAITING_APPROVAL
    with pytest.raises(IllegalTransition):  # cannot slip past to queue with the old approval
        repo.queue_task(t.task_id, t2.version)


def test_decision_on_pending_approval_made_stale_by_amendment_is_refused(repo):
    t = _awaiting(repo)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    repo.amend_task(t.task_id, make_contract(idempotency_key=t.idempotency_key, research_objective="v2"), t.version, HUMAN)
    # amendment already invalidated it; deciding it must fail and never grant
    with pytest.raises(GuardFailed, match="invalidated"):
        repo.decide_approval(ap.approval_id, True, HUMAN)
    assert repo.list_approvals(t.task_id)[0].status is ApprovalStatus.INVALIDATED


def test_stale_binding_detected_even_if_status_was_forced_pending(repo, db_path):
    """Belt and braces: a pending row whose binding is older than the task is refused and invalidated."""
    t = _awaiting(repo)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("UPDATE tasks SET contract_version=2, contract_sha256='f'||substr(contract_sha256,2), version=version+1 WHERE task_id=?", (t.task_id,))
    raw.close()
    with pytest.raises(StaleApproval):
        repo.decide_approval(ap.approval_id, True, HUMAN)
    assert repo.list_approvals(t.task_id)[0].status is ApprovalStatus.INVALIDATED


def test_noop_amendment_changes_nothing(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = approve_fully(repo, t)
    same = repo.amend_task(t.task_id, t.contract, t.version, HUMAN)
    assert same == t and {a.status for a in repo.list_approvals(t.task_id)} == {ApprovalStatus.GRANTED}


def test_amendment_rules(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    with pytest.raises(GuardFailed, match="identity"):
        repo.amend_task(t.task_id, make_contract(), t.version, HUMAN)               # different idempotency_key
    with pytest.raises(NotAuthorized):
        repo.amend_task(t.task_id, make_contract(idempotency_key=t.idempotency_key, research_objective="x"), t.version, EXECUTOR)
    with pytest.raises(VersionConflict):
        repo.amend_task(t.task_id, make_contract(idempotency_key=t.idempotency_key, research_objective="x"), t.version + 5, HUMAN)
    cancelled = repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version)
    with pytest.raises(GuardFailed, match="cannot amend"):
        repo.amend_task(t.task_id, make_contract(idempotency_key=t.idempotency_key, research_objective="y"), cancelled.version, HUMAN)


def test_only_humans_can_decide_service_and_database_level(repo, db_path):
    t = _awaiting(repo)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    for actor in (EXECUTOR, PLANNER, DIRECTOR, SYSTEM):
        with pytest.raises(NotAuthorized):
            repo.decide_approval(ap.approval_id, True, actor)
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="human"):
        raw.execute("UPDATE approvals SET status='granted', decided_by='executor-1' WHERE approval_id=?", (ap.approval_id,))
    with pytest.raises(sqlite3.DatabaseError, match="human"):
        raw.execute("UPDATE approvals SET status='granted', decided_by=NULL WHERE approval_id=?", (ap.approval_id,))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw.execute("UPDATE approvals SET task_version=9 WHERE approval_id=?", (ap.approval_id,))
    raw.close()
    assert repo.list_approvals(t.task_id)[0].status is ApprovalStatus.PENDING


def test_denial_cancels_and_revokes_others(repo):
    t = _awaiting(repo)
    a1 = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    a2 = repo.request_approval(t.task_id, ApprovalType.EXECUTE, SYSTEM)
    repo.decide_approval(a1.approval_id, False, HUMAN, "no")
    out = repo.transition(t.task_id, Event.DENY, HUMAN, t.version)
    assert out.state is TaskState.CANCELLED
    st = {a.approval_id: a.status for a in repo.list_approvals(t.task_id)}
    assert st[a1.approval_id] is ApprovalStatus.DENIED and st[a2.approval_id] is ApprovalStatus.REVOKED


def test_approval_expiry_with_fake_clock(open_repo, clock):
    repo = open_repo(limits=Limits(approval_ttl_ms=1000))
    t = _awaiting(repo)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    with pytest.raises(GuardFailed, match="expired"):
        repo.transition(t.task_id, Event.EXPIRE, SYSTEM, t.version)                  # not yet
    clock.advance(1000)
    with pytest.raises(ApprovalExpired):
        repo.decide_approval(ap.approval_id, True, HUMAN)
    assert repo.list_approvals(t.task_id)[0].status is ApprovalStatus.EXPIRED
    t = repo.get_task(t.task_id)
    ap2_ok = repo.request_approval(t.task_id, ApprovalType.EXECUTE, SYSTEM)          # a new pending, then expire it
    clock.advance(1000)
    out = repo.transition(t.task_id, Event.EXPIRE, SYSTEM, t.version)
    assert out.state is TaskState.TIMED_OUT and ap2_ok.approval_id


def test_request_approval_only_for_required_types(repo):
    t = _awaiting(repo)
    with pytest.raises(GuardFailed, match="not required"):
        repo.request_approval(t.task_id, ApprovalType.DESTRUCTIVE, SYSTEM)


