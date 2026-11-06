"""Beyond max_total_tasks_per_root (25) the new task is created but blocked in awaiting_approval until a human grants a
`limit_increase`, bound to its contract hash. A HARD cap still rejects outright."""
from __future__ import annotations

import sqlite3

import pytest

from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ApprovalStatus, ApprovalType, Role, TaskKind, contract_sha256
from airos.domain.states import Event, TaskState
from airos.errors import GuardFailed, LimitExceeded, NotAuthorized
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import AGENT_ACTORS, HUMAN, PLANNER, approve_fully, free_contract, make_contract

SMALL = Limits(max_total_tasks_per_root=5, hard_max_total_tasks_per_root=8, max_children_per_task=20, max_subtask_depth=3)


def _fill(repo, n, **contract_kw):
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
    kids = [repo.create_task(free_contract(**contract_kw), PLANNER, root.task_id).task for _ in range(n - 1)]
    return root, kids


def test_limit_allowed_limit_plus_one_blocked_approval_releases_it(open_repo):
    repo = open_repo(limits=SMALL)
    root, kids = _fill(repo, 5)                                   # exactly the limit: root + 4 = 5
    assert not any(k.over_root_limit for k in [root, *kids])
    last_ok = kids[-1]                                            # the LIMIT-th task needs no extra approval
    p = repo.transition(last_ok.task_id, Event.PLAN, PLANNER, last_ok.version, GuardContext(plan_ok=True))
    assert repo.transition(p.task_id, Event.AUTO_APPROVE, SYSTEM, p.version).state is TaskState.APPROVED

    over = repo.create_task(free_contract(), PLANNER, root.task_id).task          # limit + 1
    assert over.over_root_limit is True
    assert any(e.event_type == "task.created" and e.payload["over_root_limit"] for e in repo.iter_events())
    over = repo.transition(over.task_id, Event.PLAN, PLANNER, over.version, GuardContext(plan_ok=True))
    with pytest.raises(GuardFailed, match="zero-cost|requirements"):
        repo.transition(over.task_id, Event.AUTO_APPROVE, SYSTEM, over.version)      # even a free task cannot auto-approve
    over = repo.transition(over.task_id, Event.REQUIRE_APPROVAL, SYSTEM, over.version)
    assert over.state is TaskState.AWAITING_APPROVAL                                  # BLOCKED here
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(over.task_id, Event.APPROVE, HUMAN, over.version)
    # the approval is bound to the contract hash/version and records the worst case
    ap = repo.request_approval(over.task_id, ApprovalType.LIMIT_INCREASE, PLANNER)
    assert (ap.task_version, ap.contract_sha256) == (over.contract_version, contract_sha256(over.contract))
    for actor in (*AGENT_ACTORS, SYSTEM):
        with pytest.raises(NotAuthorized):
            repo.decide_approval(ap.approval_id, True, actor)
    assert repo.get_task(over.task_id).state is TaskState.AWAITING_APPROVAL
    repo.decide_approval(ap.approval_id, True, HUMAN, "yes, extend this objective")
    released = repo.transition(over.task_id, Event.APPROVE, HUMAN, over.version)
    assert released.state is TaskState.APPROVED


def test_the_exception_is_not_requestable_for_tasks_within_the_limit(open_repo):
    repo = open_repo(limits=SMALL)
    root, kids = _fill(repo, 3)
    t = repo.transition(kids[0].task_id, Event.PLAN, PLANNER, kids[0].version, GuardContext(plan_ok=True))
    with pytest.raises(GuardFailed, match="not required"):
        repo.request_approval(t.task_id, ApprovalType.LIMIT_INCREASE, PLANNER)


def test_changing_the_contract_invalidates_the_limit_approval_and_blocks_again(open_repo):
    repo = open_repo(limits=SMALL)
    root, _ = _fill(repo, 5)
    over = repo.create_task(free_contract(), PLANNER, root.task_id).task
    over = repo.transition(over.task_id, Event.PLAN, PLANNER, over.version, GuardContext(plan_ok=True))
    over = repo.transition(over.task_id, Event.REQUIRE_APPROVAL, SYSTEM, over.version)
    repo.decide_approval(repo.request_approval(over.task_id, ApprovalType.LIMIT_INCREASE, SYSTEM).approval_id, True, HUMAN)
    over = repo.transition(over.task_id, Event.APPROVE, HUMAN, over.version)
    assert over.state is TaskState.APPROVED
    changed = free_contract(idempotency_key=over.idempotency_key, research_objective="a different contract")
    again = repo.amend_task(over.task_id, changed, over.version, HUMAN)
    assert again.state is TaskState.AWAITING_APPROVAL                                  # blocked again
    assert {a.status for a in repo.list_approvals(over.task_id)} == {ApprovalStatus.INVALIDATED}
    assert again.over_root_limit is True


def test_database_refuses_state_change_without_the_limit_grant_via_raw_sql(open_repo, db_path):
    repo = open_repo(limits=SMALL)
    root, _ = _fill(repo, 5)
    over = repo.create_task(free_contract(), PLANNER, root.task_id).task
    over = repo.transition(over.task_id, Event.PLAN, PLANNER, over.version, GuardContext(plan_ok=True))
    over = repo.transition(over.task_id, Event.REQUIRE_APPROVAL, SYSTEM, over.version)
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="unmet approval requirements"):
        raw.execute("UPDATE tasks SET state='approved' WHERE task_id=?", (over.task_id,))
    # a grant of some OTHER type does not satisfy it either
    repo.decide_approval(repo.request_approval(over.task_id, ApprovalType.LIMIT_INCREASE, SYSTEM).approval_id, False, HUMAN)
    with pytest.raises(sqlite3.DatabaseError, match="unmet approval requirements"):
        raw.execute("UPDATE tasks SET state='approved' WHERE task_id=?", (over.task_id,))
    raw.close()


def test_over_limit_executor_task_needs_both_its_own_approvals_and_the_limit_increase(open_repo):
    repo = open_repo(limits=SMALL)
    root, _ = _fill(repo, 5)
    t = repo.create_task(make_contract(), PLANNER, root.task_id).task
    assert t.over_root_limit
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    for r in t.contract.approval_requirements:                                         # execute + cost only
        repo.decide_approval(repo.request_approval(t.task_id, ApprovalType(r.type), SYSTEM).approval_id, True, HUMAN)
    with pytest.raises(GuardFailed, match="approvals"):
        repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)                     # still missing limit_increase
    repo.decide_approval(repo.request_approval(t.task_id, ApprovalType.LIMIT_INCREASE, SYSTEM).approval_id, True, HUMAN)
    t = repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "w", "executor-1", t.version)
    assert run.attempt == 1 and repo.get_task(t.task_id).state is TaskState.RUNNING


def test_hard_cap_rejects_outright_and_other_roots_are_unaffected(open_repo):
    repo = open_repo(limits=SMALL)
    root, _ = _fill(repo, 8)                                                           # the hard cap itself
    with pytest.raises(LimitExceeded, match="hard_max_total_tasks_per_root=8"):
        repo.create_task(free_contract(), PLANNER, root.task_id)
    assert repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).created


def test_duplicate_creation_of_an_over_limit_task_is_idempotent(open_repo):
    repo = open_repo(limits=SMALL)
    root, _ = _fill(repo, 5)
    c = free_contract()
    first = repo.create_task(c, PLANNER, root.task_id)
    again = repo.create_task(c, PLANNER, root.task_id)
    assert first.created and not again.created and again.task.task_id == first.task.task_id and again.task.over_root_limit


def test_default_limits_are_25_and_100():
    assert (Limits().max_total_tasks_per_root, Limits().hard_max_total_tasks_per_root) == (25, 100)


def test_within_limit_task_still_auto_approves_and_executor_flow_is_unchanged(open_repo):
    repo = open_repo(limits=SMALL)
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    assert t.state is TaskState.APPROVED and not t.over_root_limit
