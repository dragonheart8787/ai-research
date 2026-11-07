"""Guards referenced by name from the transition table. Pure functions of GuardInput.

A guard returns None when satisfied, or a human-readable failure message.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from airos.domain.limits import Limits
from airos.domain.schemas import Role, TaskRecord
from airos.domain.states import Actor, ActorKind, BlockReason, TaskState

# Roles whose output may only be accepted by a human (PROPOSAL T23).
HUMAN_REVIEW_ROLES = frozenset({Role.DIRECTOR, Role.PLANNER, Role.CHATGPT_WORK, Role.AI_SCIENTIST})


@dataclass(frozen=True)
class GuardContext:
    """Facts only the caller (M2 services) can know. Defaults are the restrictive value."""

    plan_ok: bool = False
    verifier_passed: bool = False
    error_retryable: bool = False
    blocker_cleared: bool = False
    cascade: bool = False
    block_reason: BlockReason | None = None
    not_before_ms: int | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Facts:
    """Facts the repository computes from the database inside the transition's transaction."""

    approvals_valid: bool
    approvals_expired: bool
    deps_done: bool
    producer_id: str | None
    children_all_completed: bool
    has_children: bool
    ms_in_state: int
    now_ms: int
    reservation_held: bool = False
    required_types: frozenset[str] = frozenset()  # contract requirements + limit_increase when over the per-root limit
    side_effectful: bool = False


@dataclass(frozen=True)
class GuardInput:
    task: TaskRecord
    actor: Actor
    ctx: GuardContext
    facts: Facts
    limits: Limits


Guard = Callable[[GuardInput], str | None]


def _requirements(i: GuardInput) -> bool:
    return bool(i.facts.required_types)


def plan_valid(i: GuardInput) -> str | None:
    return None if i.ctx.plan_ok else "plan not validated (ctx.plan_ok is False)"


def needs_approval(i: GuardInput) -> str | None:
    return None if _requirements(i) else "task has no approval requirements"


def auto_approvable(i: GuardInput) -> str | None:
    c = i.task.contract
    if _requirements(i):
        return "task has approval requirements"
    if c.side_effect_class.value != "none" or c.resource_limits.max_usd_micro != 0:
        return "auto-approval only for zero-cost, side-effect-free tasks"
    return None


def approvals_valid(i: GuardInput) -> str | None:
    return None if i.facts.approvals_valid else "required approvals are missing or stale"


def approval_expired(i: GuardInput) -> str | None:
    return None if i.facts.approvals_expired else "no pending approval has expired"


def approvals_stale(i: GuardInput) -> str | None:
    if not _requirements(i):
        return "task has no approval requirements"
    return None if not i.facts.approvals_valid else "approvals are still valid"


def queue_ok(i: GuardInput) -> str | None:
    if not i.facts.deps_done:
        return "dependencies not completed"
    if not i.facts.approvals_valid:
        return "approvals are missing or stale"
    return None


def block_reason(i: GuardInput) -> str | None:
    return None if i.ctx.block_reason is not None else "ctx.block_reason is required"


def start_ok(i: GuardInput) -> str | None:
    t = i.task.not_before_ms
    if t is not None and t > i.facts.now_ms:
        return "not_before has not elapsed"
    return None


def finish_ok(i: GuardInput) -> str | None:
    if i.task.kind.value == "objective":
        if not i.facts.has_children or not i.facts.children_all_completed:
            return "objective needs >=1 child and all children completed"
        return None
    return None if i.ctx.verifier_passed else "verifier has not passed"


def retry_allowed(i: GuardInput) -> str | None:
    if not i.ctx.error_retryable:
        return "error is not retryable"
    if i.facts.side_effectful:
        return "side-effectful tasks are never auto-retried"
    if i.task.attempt_count - i.task.uncounted_attempts >= 1 + i.task.contract.resource_limits.max_retries:
        return "retries exhausted"
    return None


def reviewer_ok(i: GuardInput) -> str | None:
    if i.task.review_hold and i.actor.kind is not ActorKind.HUMAN:
        return "billing-integrity hold: only a human may review this task"
    if i.facts.producer_id is not None and i.actor.id == i.facts.producer_id:
        return "reviewer must not be the producer of the run under review"
    needs_human = i.task.contract.human_review_required or i.task.assigned_role in HUMAN_REVIEW_ROLES
    if needs_human and i.actor.kind is not ActorKind.HUMAN:
        return "this task requires a human reviewer"
    return None


def rework_allowed(i: GuardInput) -> str | None:
    r = reviewer_ok(i)
    if r:
        return r
    if i.task.review_cycles >= i.limits.max_review_cycles:
        return "review cycles exhausted"
    if i.task.attempt_count - i.task.uncounted_attempts >= 1 + i.task.contract.resource_limits.max_retries:
        return "attempts exhausted"
    return None


def review_ttl(i: GuardInput) -> str | None:
    return None if i.facts.ms_in_state >= i.limits.review_ttl_ms else "review TTL not reached"


def blocked_ttl(i: GuardInput) -> str | None:
    return None if i.facts.ms_in_state >= i.limits.blocked_ttl_ms else "blocked TTL not reached"


def unblock_ok(i: GuardInput) -> str | None:
    reason = i.task.blocked_reason
    if reason is BlockReason.DEPENDENCY_FAILED:
        return "dependency_failed cannot be resumed; cancel instead"
    if reason is BlockReason.ADAPTER_UNAVAILABLE:
        if not i.ctx.blocker_cleared:
            return "adapter still unavailable"
    elif i.actor.kind is not ActorKind.HUMAN:
        return f"blocked_reason={reason.value if reason else None} can only be resolved by a human"
    if not i.facts.deps_done:
        return "dependencies not completed"
    if i.task.blocked_from in (TaskState.APPROVED, TaskState.QUEUED, TaskState.RUNNING) and not i.facts.approvals_valid:
        return "approvals are missing or stale"
    return None


def reservation_held(i: GuardInput) -> str | None:
    """Reads the ledger (via Facts, computed in the same transaction): an OPEN task-level reserve row must exist."""
    if i.task.kind.value == "objective":
        return None  # containers have no run and no spend
    if i.facts.reservation_held:
        return None
    return f"no open ledger reservation for attempt {i.task.attempt_count + 1}"


def cascade_only(i: GuardInput) -> str | None:
    return None if i.ctx.cascade else "cascade_cancel is only valid as part of a parent cancellation"


GUARDS: dict[str, Guard] = {
    g.__name__: g
    for g in (
        plan_valid, needs_approval, auto_approvable, approvals_valid, approval_expired, approvals_stale,
        queue_ok, block_reason, start_ok, finish_ok, retry_allowed, reviewer_ok, rework_allowed,
        review_ttl, blocked_ttl, unblock_ok, cascade_only, reservation_held,
    )
}


def run_guards(names: tuple[str, ...], i: GuardInput) -> tuple[str, str] | None:
    """Evaluate guards in order; return (guard_name, message) of the first failure, else None."""
    for name in names:
        msg = GUARDS[name](i)
        if msg is not None:
            return name, msg
    return None


def run_guard(name: str | None, i: GuardInput) -> str | None:
    return None if name is None else GUARDS[name](i)
