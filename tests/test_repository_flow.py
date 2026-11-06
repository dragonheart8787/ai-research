from __future__ import annotations

import sqlite3

import pytest

from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.machine import allowed_state_pairs
from airos.domain.schemas import AcceptanceCriterion, Role, TaskState
from airos.domain.states import Actor, ActorKind, BlockReason, Event
from airos.errors import (
    ActorNotAllowed,
    GuardFailed,
    LimitExceeded,
    NotAuthorized,
    SecretInContract,
)
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import (
    DIRECTOR,
    EXECUTOR,
    HUMAN,
    PLANNER,
    approve_fully,
    free_contract,
    make_contract,
    to_running,
)


def test_happy_path_to_completed(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    assert t.state is TaskState.DRAFT and t.version == 1
    t, _ = to_running(repo, t)
    run = repo.list_runs(t.task_id)[0]
    t = repo.complete_run(run.run_id, "w1", __import__("airos.domain.states", fromlist=["RunState"]).RunState.SUCCEEDED,
                          verifier_passed=True)
    assert t.state is TaskState.REVIEW
    with pytest.raises(ActorNotAllowed):  # executor cannot accept its own output
        repo.transition(t.task_id, Event.ACCEPT, EXECUTOR, t.version)
    t = repo.transition(t.task_id, Event.ACCEPT, DIRECTOR, t.version)
    assert t.state is TaskState.COMPLETED
    assert repo.verify_chain().ok


def test_auto_approve_path_for_zero_cost_task(repo):
    t = repo.create_task(free_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    with pytest.raises(GuardFailed):  # has no requirements -> cannot require approval
        repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    t = repo.transition(t.task_id, Event.AUTO_APPROVE, SYSTEM, t.version)
    assert t.state is TaskState.APPROVED


def test_costly_task_cannot_be_auto_approved(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    with pytest.raises(GuardFailed):
        repo.transition(t.task_id, Event.AUTO_APPROVE, SYSTEM, t.version)


def test_agents_and_system_cannot_approve(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    for a in (PLANNER, DIRECTOR, EXECUTOR, SYSTEM):
        with pytest.raises(ActorNotAllowed):
            repo.transition(t.task_id, Event.APPROVE, a, t.version)
    with pytest.raises(GuardFailed):  # human, but nothing granted yet
        repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)


def test_unregistered_or_mismatched_principal_rejected(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    with pytest.raises(NotAuthorized):
        repo.transition(t.task_id, Event.CANCEL, Actor(ActorKind.HUMAN, "ghost"), t.version)
    with pytest.raises(NotAuthorized):  # planner-1 is an agent, not a human
        repo.transition(t.task_id, Event.CANCEL, Actor(ActorKind.HUMAN, PLANNER.id), t.version)


def test_director_must_not_review_director_output_human_required(repo):
    t = repo.create_task(make_contract(assigned_role=Role.DIRECTOR, approval_requirements=[
        {"type": "cost", "reason": "x"}]), HUMAN).task
    # fast-forward via raw legal transitions
    t = approve_fully(repo, t)
    t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    run = repo.start_run(t.task_id, "w1", DIRECTOR.id, t.version, GuardContext(budget_reserved=True))
    from airos.domain.states import RunState
    t = repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    with pytest.raises(GuardFailed):
        repo.transition(t.task_id, Event.ACCEPT, DIRECTOR, t.version)  # producer == reviewer / human required
    assert repo.transition(t.task_id, Event.ACCEPT, HUMAN, t.version).state is TaskState.COMPLETED


def test_db_trigger_rejects_illegal_state_change_even_with_raw_sql(repo, db_path):
    t = repo.create_task(make_contract(), HUMAN).task
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="illegal state transition"):
        raw.execute("UPDATE tasks SET state='completed' WHERE task_id=?", (t.task_id,))
    # terminal states cannot be left either
    c = repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version)
    assert c.state is TaskState.CANCELLED
    with pytest.raises(sqlite3.DatabaseError, match="illegal state transition"):
        raw.execute("UPDATE tasks SET state='draft' WHERE task_id=?", (t.task_id,))
    with pytest.raises(sqlite3.DatabaseError, match="version must not decrease"):
        raw.execute("UPDATE tasks SET version=0+version-1 WHERE task_id=?", (t.task_id,))
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        raw.execute("DELETE FROM tasks WHERE task_id=?", (t.task_id,))
    raw.close()


def test_db_transition_table_equals_code_table(repo):
    rows = {(r[0], r[1]) for r in repo._conn().execute("SELECT from_state,to_state FROM allowed_transitions")}
    assert rows == allowed_state_pairs()


def test_failed_transition_leaves_no_audit_trace_and_no_state_change(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    head = repo.head()
    with pytest.raises(GuardFailed):
        repo.transition(t.task_id, Event.PLAN, PLANNER, t.version)  # plan_ok False
    assert repo.head() == head
    assert repo.get_task(t.task_id).version == t.version


def test_creation_permissions(repo):
    with pytest.raises(NotAuthorized):
        repo.create_task(make_contract(), EXECUTOR)
    with pytest.raises(NotAuthorized):
        repo.create_task(make_contract(), PLANNER)  # planner needs a parent
    with pytest.raises(NotAuthorized):
        repo.create_task(make_contract(), DIRECTOR)  # director_may_create_root defaults False
    root = repo.create_task(make_contract(), HUMAN).task
    child = repo.create_task(make_contract(), PLANNER, parent_task_id=root.task_id).task
    assert child.parent_task_id == root.task_id and child.depth == 1 and child.root_task_id == root.task_id


def test_recursion_limits(open_repo):
    repo = open_repo(limits=Limits(max_subtask_depth=2, max_children_per_task=2, max_total_tasks_per_root=4))
    root = repo.create_task(make_contract(), HUMAN).task
    a = repo.create_task(make_contract(), PLANNER, root.task_id).task
    repo.create_task(make_contract(), PLANNER, root.task_id)
    with pytest.raises(LimitExceeded, match="children"):
        repo.create_task(make_contract(), PLANNER, root.task_id)
    b = repo.create_task(make_contract(), PLANNER, a.task_id).task  # depth 2 ok; total now 4
    with pytest.raises(LimitExceeded, match="depth"):
        repo.create_task(make_contract(), PLANNER, b.task_id)
    with pytest.raises(LimitExceeded, match="total"):
        repo.create_task(make_contract(), PLANNER, a.task_id)


def test_cancel_cascades_to_descendants_and_closes_running_run(repo):
    root = repo.create_task(make_contract(), HUMAN).task
    kid = repo.create_task(make_contract(), PLANNER, root.task_id).task
    grandkid = repo.create_task(make_contract(), PLANNER, kid.task_id).task
    kid, run_id = to_running(repo, kid)
    out = repo.transition(root.task_id, Event.CANCEL, HUMAN, root.version)
    assert out.state is TaskState.CANCELLED
    for t in (kid, grandkid):
        assert repo.get_task(t.task_id).state is TaskState.CANCELLED
    assert repo.list_runs(kid.task_id)[0].state.value == "cancelled"
    assert repo.verify_chain().ok


def test_cascade_event_cannot_be_used_directly(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    with pytest.raises(ActorNotAllowed):
        repo.transition(t.task_id, Event.CASCADE_CANCEL, HUMAN, t.version, GuardContext(cascade=True))
    with pytest.raises(GuardFailed):
        repo.transition(t.task_id, Event.CASCADE_CANCEL, SYSTEM, t.version)


def test_dependencies_gate_queueing_and_detect_cycles(repo):
    a = repo.create_task(make_contract(), HUMAN).task
    b = repo.create_task(make_contract(), HUMAN).task
    repo.add_dependency(b.task_id, a.task_id, HUMAN)
    with pytest.raises(GuardFailed, match="cycle"):
        repo.add_dependency(a.task_id, b.task_id, HUMAN)
    b = repo.get_task(b.task_id)
    b = approve_fully(repo, b)
    with pytest.raises(GuardFailed, match="dependencies"):
        repo.transition(b.task_id, Event.QUEUE, SYSTEM, b.version)


def test_blocked_unblock_rules(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = approve_fully(repo, t)
    t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    t = repo.transition(t.task_id, Event.BLOCK, SYSTEM, t.version, GuardContext(block_reason=BlockReason.BUDGET_INSUFFICIENT))
    assert t.blocked_reason is BlockReason.BUDGET_INSUFFICIENT and t.blocked_from is TaskState.QUEUED
    with pytest.raises(GuardFailed, match="human"):
        repo.transition(t.task_id, Event.UNBLOCK, SYSTEM, t.version)
    t = repo.transition(t.task_id, Event.UNBLOCK, HUMAN, t.version)
    assert t.state is TaskState.QUEUED and t.blocked_reason is None


def test_secret_in_contract_refused(open_repo):
    from airos.redaction import Redactor

    secret = "contract-" + "secret-" + "value-9981"
    repo = open_repo(redactor=Redactor([secret]))
    with pytest.raises(SecretInContract):
        repo.create_task(make_contract(research_objective=f"use key {secret}"), HUMAN)
    assert repo.list_tasks() == []


def test_acceptance_criteria_roundtrip_through_db(repo):
    c = make_contract(acceptance_criteria=[AcceptanceCriterion(id="k1", description="d", mode="human")])
    t = repo.create_task(c, HUMAN).task
    assert repo.get_task(t.task_id).contract == c
