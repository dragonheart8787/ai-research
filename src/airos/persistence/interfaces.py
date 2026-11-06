"""Repository interfaces. Services depend on these Protocols, never on sqlite3 (ADR-0001).

The write methods are the ONLY way state changes; each runs in one atomic transaction together with
its audit event(s). There is deliberately no update/delete method for audit events or ledger entries.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from airos.audit import AnchorVerification, ChainVerification
from airos.domain.guards import GuardContext
from airos.domain.schemas import (
    Approval,
    ApprovalType,
    Artifact,
    AuditEvent,
    BudgetLedgerEntry,
    ErrorRecord,
    Run,
    TaskContract,
    TaskRecord,
)
from airos.domain.states import Actor, Event, RunState, TaskState


@dataclass(frozen=True)
class CreateResult:
    task: TaskRecord
    created: bool  # False => idempotent duplicate; the existing task is returned


@dataclass(frozen=True)
class RecoveryAction:
    run_id: str
    task_id: str
    action: str  # "requeued" | "blocked" | "failed"
    reason: str


@dataclass(frozen=True)
class RecoveryReport:
    actions: list[RecoveryAction] = field(default_factory=list)

    def by_action(self, action: str) -> list[RecoveryAction]:
        return [a for a in self.actions if a.action == action]


@dataclass(frozen=True)
class AuditHead:
    seq: int
    entry_hash: str


class TaskRepository(Protocol):
    def register_principal(self, principal_id: str, type: str, role: str | None, display_name: str) -> None: ...
    def create_task(self, contract: TaskContract, creator: Actor, parent_task_id: str | None = None) -> CreateResult: ...
    def get_task(self, task_id: str) -> TaskRecord: ...
    def find_task_by_key(self, idempotency_key: str) -> TaskRecord | None: ...
    def list_tasks(self, state: TaskState | None = None) -> list[TaskRecord]: ...
    def add_dependency(self, task_id: str, depends_on_task_id: str, actor: Actor) -> None: ...
    def amend_task(self, task_id: str, new_contract: TaskContract, expected_version: int, actor: Actor) -> TaskRecord: ...
    def transition(
        self, task_id: str, event: Event, actor: Actor, expected_version: int,
        ctx: GuardContext | None = None, *, reason: str | None = None, error: ErrorRecord | None = None,
    ) -> TaskRecord: ...


class ApprovalRepository(Protocol):
    def request_approval(self, task_id: str, approval_type: ApprovalType, requested_by: Actor) -> Approval: ...
    def decide_approval(self, approval_id: str, grant: bool, decided_by: Actor, reason: str | None = None) -> Approval: ...
    def list_approvals(self, task_id: str) -> list[Approval]: ...


class RunRepository(Protocol):
    def start_run(self, task_id: str, worker_id: str, agent_id: str, expected_version: int, ctx: GuardContext) -> Run: ...
    def heartbeat(self, run_id: str, worker_id: str) -> bool: ...
    def complete_run(
        self, run_id: str, worker_id: str, outcome: RunState, *, retryable: bool = False,
        verifier_passed: bool = False, error: ErrorRecord | None = None,
    ) -> TaskRecord: ...
    def recover_leases(self, current_worker_id: str | None = None) -> RecoveryReport: ...
    def list_runs(self, task_id: str) -> list[Run]: ...


class AuditRepository(Protocol):
    def head(self) -> AuditHead: ...
    def iter_events(self, from_seq: int = 1) -> Iterator[AuditEvent]: ...
    def verify_chain(self) -> ChainVerification: ...
    def verify_chain_with_anchors(self) -> AnchorVerification: ...


class ArtifactRepository(Protocol):
    def add_artifact(self, artifact: Artifact, actor: Actor) -> Artifact: ...
    def get_artifact(self, artifact_id: str) -> Artifact: ...


class LedgerRepository(Protocol):
    def append_ledger_entry(self, entry: BudgetLedgerEntry, actor: Actor) -> BudgetLedgerEntry: ...
    def list_ledger(self, budget_id: str | None = None) -> list[BudgetLedgerEntry]: ...


class Repository(TaskRepository, ApprovalRepository, RunRepository, AuditRepository, ArtifactRepository, LedgerRepository, Protocol):
    def close(self) -> None: ...
