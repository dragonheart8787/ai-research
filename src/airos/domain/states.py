"""Enumerations shared by the state machine, schemas and persistence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskState(str, Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.TIMED_OUT, TaskState.CANCELLED}
)
NON_TERMINAL_STATES = frozenset(set(TaskState) - TERMINAL_STATES)


class Event(str, Enum):
    PLAN = "plan"
    CANCEL = "cancel"
    CASCADE_CANCEL = "cascade_cancel"
    REQUIRE_APPROVAL = "require_approval"
    AUTO_APPROVE = "auto_approve"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    DENY = "deny"
    EXPIRE = "expire"
    QUEUE = "queue"
    REAPPROVE = "reapprove"
    BLOCK = "block"
    START = "start"
    FINISH = "finish"
    RETRY = "retry"
    FAIL = "fail"
    TIMEOUT = "timeout"
    ACCEPT = "accept"
    REWORK = "rework"
    REJECT = "reject"
    REVIEW_TIMEOUT = "review_timeout"
    UNBLOCK = "unblock"
    BLOCK_TIMEOUT = "block_timeout"


class ActorKind(str, Enum):
    HUMAN = "human"
    SYSTEM = "system"
    DIRECTOR = "director"
    PLANNER = "planner"
    EXECUTOR = "executor"
    CHATGPT_WORK = "chatgpt_work"
    AI_SCIENTIST = "ai_scientist"


AGENT_KINDS = frozenset(
    {
        ActorKind.DIRECTOR,
        ActorKind.PLANNER,
        ActorKind.EXECUTOR,
        ActorKind.CHATGPT_WORK,
        ActorKind.AI_SCIENTIST,
    }
)


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str


class BlockReason(str, Enum):
    BUDGET_INSUFFICIENT = "budget_insufficient"
    DEPENDENCY_FAILED = "dependency_failed"
    NEEDS_INPUT = "needs_input"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    CRASH_RECOVERY_DECISION = "crash_recovery_decision"
    BUDGET_EXHAUSTED = "budget_exhausted"


class SideEffectClass(str, Enum):
    NONE = "none"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"


# "Side-effectful" = effects that escape the per-run workspace. Never auto-rerun (PROPOSAL 8.5).
SIDE_EFFECTFUL = frozenset({SideEffectClass.EXTERNAL_WRITE, SideEffectClass.DESTRUCTIVE})


class RunState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
