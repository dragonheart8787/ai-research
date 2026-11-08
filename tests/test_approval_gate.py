"""Approval blocks execution: an unapproved task can never reach approved/queued/running by ANY path."""
from __future__ import annotations

import contextlib
import sqlite3

import pytest

from airos.domain import machine
from airos.domain.guards import GuardContext
from airos.domain.schemas import ApprovalType
from airos.domain.states import BlockReason, Event, TaskState
from airos.errors import AirosError, GuardFailed
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import AGENT_ACTORS, EXECUTOR, HUMAN, PLANNER, make_contract

ACTORS = [HUMAN, SYSTEM, *AGENT_ACTORS]
PERMISSIVE = GuardContext(plan_ok=True, verifier_passed=True, error_retryable=True, blocker_cleared=True,
                          block_reason=BlockReason.BUDGET_INSUFFICIENT, cascade=True)
FORBIDDEN = {TaskState.APPROVED, TaskState.QUEUED, TaskState.RUNNING, TaskState.REVIEW, TaskState.COMPLETED}


def _replay(repo, path):
    t = repo.create_task(make_contract(), HUMAN).task
    for event, actor in path:
        t = repo.transition(t.task_id, event, actor, t.version, PERMISSIVE)
    return t


def test_exhaustive_exploration_of_an_unapproved_task_never_reaches_execution(repo):
    """Breadth-first over every (legal event x actor) from every state reachable WITHOUT a human approval grant."""
    reach: dict[TaskState, list] = {TaskState.DRAFT: []}
    changed = True
    while changed:
        changed = False
        for state, path in list(reach.items()):
            for event in Event:
                if machine.lookup(state, event) is None:
                    continue
                for actor in ACTORS:
                    t = _replay(repo, path)
                    assert t.state is state
                    try:
                        if event in (Event.QUEUE, Event.UNBLOCK, Event.REWORK):
                            out = repo.enqueue(t.task_id, event, actor, t.version, PERMISSIVE)
                        else:
                            out = repo.transition(t.task_id, event, actor, t.version, PERMISSIVE)
                    except AirosError:
                        continue
                    if out.state not in reach:
                        reach[out.state] = [*path, (event, actor)]
                        changed = True
            # start_run can never rescue an unapproved task either
            t = _replay(repo, path)
            with pytest.raises(AirosError):
                repo.start_run(t.task_id, "w", EXECUTOR.id, t.version)
    assert not (set(reach) & FORBIDDEN), set(reach) & FORBIDDEN
    assert TaskState.AWAITING_APPROVAL in reach and TaskState.CANCELLED in reach  # the exploration did move around


def test_partial_approval_is_not_enough(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    ap = repo.request_approval(t.task_id, ApprovalType.EXECUTE, SYSTEM)
    repo.decide_approval(ap.approval_id, True, HUMAN)                      # cost is still pending
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)


def test_database_refuses_approved_queued_running_with_unmet_requirements_via_raw_sql(repo, db_path):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="unmet approval requirements"):
        raw.execute("UPDATE tasks SET state='approved' WHERE task_id=?", (t.task_id,))
    # forging a 'granted' row with an AGENT decider (trigger) or with a stale binding does not help either
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    ap2 = repo.request_approval(t.task_id, ApprovalType.EXECUTE, SYSTEM)
    with pytest.raises(sqlite3.DatabaseError, match="human"):
        raw.execute("UPDATE approvals SET status='granted', decided_by='executor-1' WHERE approval_id=?", (ap.approval_id,))
    raw.execute("UPDATE approvals SET status='granted', decided_by='human-1', contract_sha256=contract_sha256 WHERE approval_id IN (?,?)",
                (ap.approval_id, ap2.approval_id))
    raw.execute("UPDATE tasks SET state='approved' WHERE task_id=?", (t.task_id,))   # legit: both granted, bound, human
    raw.close()


def test_stale_grants_do_not_satisfy_the_gate(repo, db_path):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    for r in t.contract.approval_requirements:
        repo.decide_approval(repo.request_approval(t.task_id, r.type, SYSTEM).approval_id, True, HUMAN)
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("UPDATE tasks SET contract_version=2, contract_sha256='f'||substr(contract_sha256,2), version=version+1 WHERE task_id=?",
                (t.task_id,))   # the contract moved on after the grants (simulated at the SQL level)
    with pytest.raises(sqlite3.DatabaseError, match="unmet approval requirements"):
        raw.execute("UPDATE tasks SET state='approved' WHERE task_id=?", (t.task_id,))
    raw.close()
    with contextlib.suppress(AirosError):
        pass
    cur = repo.get_task(t.task_id)
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(cur.task_id, Event.APPROVE, HUMAN, cur.version)
