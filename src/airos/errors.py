"""Exception hierarchy. Every failure mode the tests assert on has its own type."""
from __future__ import annotations


class AirosError(Exception):
    """Base class."""


class IllegalTransition(AirosError):
    def __init__(self, state: str, event: str) -> None:
        super().__init__(f"no transition for state={state!r} event={event!r}")
        self.state, self.event = state, event




class GuardFailed(AirosError):
    def __init__(self, guard: str, message: str) -> None:
        super().__init__(f"guard {guard!r} failed: {message}")
        self.guard, self.message = guard, message


class VersionConflict(AirosError):
    """Compare-and-swap on tasks.version lost."""


class IdempotencyConflict(AirosError):
    """Same idempotency_key re-used with a different contract."""


class NotAuthorized(AirosError):
    """Principal missing/disabled/mismatched, or action not permitted for the actor."""


class ActorNotAllowed(NotAuthorized):
    """The transition table does not allow this actor kind to trigger the event."""

    def __init__(self, tid: str, actor: str) -> None:
        super().__init__(f"actor kind {actor!r} may not trigger transition {tid}")
        self.tid, self.actor = tid, actor


class BudgetInsufficient(GuardFailed):
    """A reservation could not be made (task, root, global-daily or role level)."""

    def __init__(self, reason: str, credit: bool = False) -> None:
        super().__init__("budget_reservation", reason)
        self.credit = credit


class AdapterUnavailable(AirosError):
    """Unknown role, disabled adapter, or adapter without credentials/implementation: fail closed."""


class IntegrityError(AirosError):
    """Stored bytes or provenance do not match their recorded hash."""


class UnsafePath(AirosError):
    """Path traversal, symlink, hard link, or non-regular file."""


class NotFound(AirosError):
    pass


class StaleApproval(AirosError):
    """Approval is bound to an older (contract_version, contract_sha256)."""


class ApprovalExpired(AirosError):
    pass


class LeaseLost(AirosError):
    """Worker no longer holds a valid lease; its result must be discarded."""


class LimitExceeded(AirosError):
    """Depth / children / total-task limit."""


class SecretInContract(AirosError):
    """A secret-looking value was found in data that must be stored verbatim."""


class SchemaMismatch(AirosError):
    """Database schema/migration/transition table does not match the code."""
