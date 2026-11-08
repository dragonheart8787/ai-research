"""Data-driven task state machine. The transition table is DATA; there is no if/else chain.

Each row: (id, from_state, event, to_state, allowed actor kinds, guard name | None).
Guards live in airos.domain.guards and are looked up by name.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from airos.domain.states import (
    NON_TERMINAL_STATES,
    ActorKind,
    Event,
    TaskState,
)
from airos.errors import ActorNotAllowed, IllegalTransition

S, E, A = TaskState, Event, ActorKind


@dataclass(frozen=True)
class Transition:
    tid: str
    from_state: TaskState
    event: Event
    to_state: TaskState
    actors: frozenset[ActorKind]
    guards: tuple[str, ...] = ()


def _t(tid: str, f: TaskState, e: Event, t: TaskState, actors: set[ActorKind], *guards: str) -> Transition:
    return Transition(tid, f, e, t, frozenset(actors), tuple(guards))


_H, _SY, _D, _P = A.HUMAN, A.SYSTEM, A.DIRECTOR, A.PLANNER  # no agent role may cancel or approve

TRANSITIONS: tuple[Transition, ...] = (
    _t("T01", S.DRAFT, E.PLAN, S.PLANNED, {_P, _H}, "plan_valid"),
    _t("T02", S.DRAFT, E.CANCEL, S.CANCELLED, {_H}),
    _t("T03", S.PLANNED, E.REQUIRE_APPROVAL, S.AWAITING_APPROVAL, {_SY}, "needs_approval"),
    _t("T04", S.PLANNED, E.AUTO_APPROVE, S.APPROVED, {_SY}, "auto_approvable"),
    _t("T05", S.PLANNED, E.CANCEL, S.CANCELLED, {_H}),
    _t("T06", S.AWAITING_APPROVAL, E.APPROVE, S.APPROVED, {_H}, "approvals_valid"),
    _t("T07", S.AWAITING_APPROVAL, E.REQUEST_CHANGES, S.DRAFT, {_H}),
    _t("T08", S.AWAITING_APPROVAL, E.DENY, S.CANCELLED, {_H}),
    _t("T08b", S.AWAITING_APPROVAL, E.CANCEL, S.CANCELLED, {_H}),
    _t("T09", S.AWAITING_APPROVAL, E.EXPIRE, S.TIMED_OUT, {_SY}, "approval_expired"),
    _t("T10", S.APPROVED, E.QUEUE, S.QUEUED, {_SY}, "queue_ok", "reservation_held"),
    _t("T11", S.APPROVED, E.REAPPROVE, S.AWAITING_APPROVAL, {_SY}, "approvals_stale"),
    _t("T12", S.APPROVED, E.CANCEL, S.CANCELLED, {_H}),
    _t("T13", S.APPROVED, E.BLOCK, S.BLOCKED, {_SY}, "block_reason"),
    _t("T14", S.QUEUED, E.START, S.RUNNING, {_SY}, "start_ok", "reservation_held"),
    _t("T15", S.QUEUED, E.BLOCK, S.BLOCKED, {_SY}, "block_reason"),
    _t("T16", S.QUEUED, E.CANCEL, S.CANCELLED, {_H}),
    _t("T17", S.RUNNING, E.FINISH, S.REVIEW, {_SY}, "finish_ok"),
    _t("T18", S.RUNNING, E.RETRY, S.QUEUED, {_SY}, "retry_allowed", "reservation_held"),
    _t("T19", S.RUNNING, E.FAIL, S.FAILED, {_SY}),
    _t("T20", S.RUNNING, E.TIMEOUT, S.TIMED_OUT, {_SY}),
    _t("T21", S.RUNNING, E.BLOCK, S.BLOCKED, {_SY}, "block_reason"),
    _t("T22", S.RUNNING, E.CANCEL, S.CANCELLED, {_H}),
    _t("T23", S.REVIEW, E.ACCEPT, S.COMPLETED, {_D, _H}, "reviewer_ok"),
    _t("T24", S.REVIEW, E.REWORK, S.QUEUED, {_D, _H}, "rework_allowed", "reservation_held"),
    _t("T25", S.REVIEW, E.REJECT, S.FAILED, {_D, _H}, "reviewer_ok"),
    _t("T26", S.REVIEW, E.CANCEL, S.CANCELLED, {_H}),
    _t("T27", S.REVIEW, E.REVIEW_TIMEOUT, S.TIMED_OUT, {_SY}, "review_ttl"),
    _t("T28", S.BLOCKED, E.UNBLOCK, S.QUEUED, {_SY, _H}, "unblock_ok", "reservation_held"),
    _t("T29", S.BLOCKED, E.CANCEL, S.CANCELLED, {_H}),
    _t("T30", S.BLOCKED, E.BLOCK_TIMEOUT, S.TIMED_OUT, {_SY}, "blocked_ttl"),
    # Parent cancellation fans out to every non-terminal descendant (system actor only).
    *(
        _t(f"C-{st.value}", st, E.CASCADE_CANCEL, S.CANCELLED, {_SY}, "cascade_only")
        for st in sorted(NON_TERMINAL_STATES, key=lambda x: x.value)
    ),
)


def _build_index() -> dict[tuple[TaskState, Event], Transition]:
    idx: dict[tuple[TaskState, Event], Transition] = {}
    for t in TRANSITIONS:
        key = (t.from_state, t.event)
        if key in idx:
            raise ValueError(f"duplicate transition for {key}")
        idx[key] = t
    return idx


_INDEX = _build_index()


def lookup(state: TaskState, event: Event) -> Transition | None:
    return _INDEX.get((state, event))


def decide(state: TaskState, event: Event, actor: ActorKind) -> Transition:
    """Pure: returns the transition row or raises. Guards are evaluated by the caller."""
    tr = _INDEX.get((state, event))
    if tr is None:
        raise IllegalTransition(state.value, event.value)
    if actor not in tr.actors:
        raise ActorNotAllowed(tr.tid, actor.value)
    return tr


def allowed_state_pairs() -> set[tuple[str, str]]:
    return {(t.from_state.value, t.to_state.value) for t in TRANSITIONS}


def reachable_from(start: TaskState) -> set[TaskState]:
    seen = {start}
    q: deque[TaskState] = deque([start])
    while q:
        cur = q.popleft()
        for t in TRANSITIONS:
            if t.from_state is cur and t.to_state not in seen:
                seen.add(t.to_state)
                q.append(t.to_state)
    return seen


def guard_names() -> set[str]:
    return {g for t in TRANSITIONS for g in t.guards}
