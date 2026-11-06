from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from airos.domain import guards, machine
from airos.domain.states import NON_TERMINAL_STATES, TERMINAL_STATES, ActorKind, Event, TaskState
from airos.errors import ActorNotAllowed, IllegalTransition

ALL_PAIRS = list(itertools.product(TaskState, Event))


def test_table_ids_unique_and_guards_resolve():
    ids = [t.tid for t in machine.TRANSITIONS]
    assert len(ids) == len(set(ids))
    assert machine.guard_names() <= set(guards.GUARDS)
    assert all(t.actors for t in machine.TRANSITIONS)


@given(st.sampled_from(ALL_PAIRS), st.sampled_from(list(ActorKind)))
def test_property_pair_not_in_table_is_rejected(pair, actor):
    state, event = pair
    row = machine.lookup(state, event)
    if row is None:
        with pytest.raises(IllegalTransition):
            machine.decide(state, event, actor)
    elif actor in row.actors:
        assert machine.decide(state, event, actor) is row
    else:
        with pytest.raises(ActorNotAllowed):
            machine.decide(state, event, actor)


def test_exhaustive_every_unlisted_pair_rejected():
    listed = {(t.from_state, t.event) for t in machine.TRANSITIONS}
    rejected = 0
    for state, event in ALL_PAIRS:
        if (state, event) not in listed:
            for actor in ActorKind:
                with pytest.raises(IllegalTransition):
                    machine.decide(state, event, actor)
            rejected += 1
    assert rejected == len(ALL_PAIRS) - len(listed) > 0


def test_every_allowed_transition_reachable_from_draft():
    reach = machine.reachable_from(TaskState.DRAFT)
    assert reach == set(TaskState)  # every state, hence
    for t in machine.TRANSITIONS:
        assert t.from_state in reach, f"{t.tid} unreachable from draft"
        assert t.to_state in reach


def test_terminal_states_have_no_outgoing_transitions():
    for t in machine.TRANSITIONS:
        assert t.from_state not in TERMINAL_STATES
    assert {TaskState.COMPLETED, TaskState.FAILED, TaskState.TIMED_OUT, TaskState.CANCELLED} == TERMINAL_STATES


def test_every_nonterminal_state_can_be_cancelled_by_cascade():
    for st_ in NON_TERMINAL_STATES:
        assert machine.lookup(st_, Event.CASCADE_CANCEL) is not None


@given(st.sampled_from([k for k in ActorKind if k is not ActorKind.HUMAN]))
def test_property_no_non_human_can_approve_or_deny(actor):
    for event in (Event.APPROVE, Event.DENY, Event.REQUEST_CHANGES):
        for t in (x for x in machine.TRANSITIONS if x.event is event):
            assert actor not in t.actors


def test_only_system_starts_and_queues():
    for event in (Event.START, Event.QUEUE, Event.FINISH, Event.RETRY, Event.AUTO_APPROVE):
        for t in (x for x in machine.TRANSITIONS if x.event is event):
            assert t.actors == {ActorKind.SYSTEM}


def test_reviewer_independence_guard_is_pure():
    from airos.domain.guards import Facts, GuardContext, GuardInput, reviewer_ok
    from airos.domain.limits import Limits

    # build a minimal TaskRecord via the repository-independent constructor
    from airos.domain.schemas import Role, TaskKind, TaskRecord, contract_sha256
    from airos.domain.states import Actor, SideEffectClass
    from airos.persistence.sqlite_repo import SYSTEM  # noqa: F401
    from tests.conftest import make_contract

    c = make_contract()
    task = TaskRecord(task_id="t", idempotency_key=c.idempotency_key, kind=TaskKind.WORK, root_task_id="t",
                      parent_task_id=None, depth=0, assigned_role=Role.EXECUTOR, state=TaskState.REVIEW, version=1,
                      contract_version=1, blocked_reason=None, blocked_from=None, contract=c,
                      contract_sha256=contract_sha256(c), side_effect_class=SideEffectClass.NONE, attempt_count=1,
                      review_cycles=0, timeout_retries=0, not_before_ms=None, error_history=[], created_by="h", created_at_ms=0,
                      updated_at_ms=0, state_entered_at_ms=0)
    facts = Facts(True, False, True, producer_id="same-id", children_all_completed=True, has_children=False,
                  ms_in_state=0, now_ms=0)
    same = GuardInput(task, Actor(ActorKind.DIRECTOR, "same-id"), GuardContext(), facts, Limits())
    other = GuardInput(task, Actor(ActorKind.DIRECTOR, "someone-else"), GuardContext(), facts, Limits())
    assert reviewer_ok(same) is not None
    assert reviewer_ok(other) is None
