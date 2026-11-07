"""Pydantic v2 schemas. All models: extra="forbid", frozen, explicit schema_version.

Money is integer micro-USD, time is integer UTC epoch milliseconds, CPU is integer millicores
(no floats anywhere, so canonical JSON hashing is exact).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from airos.canonical import canonical_json, sha256_hex
from airos.domain.states import (
    SIDE_EFFECTFUL,
    BlockReason,
    RunState,
    SideEffectClass,
    TaskState,
)

SHA256_RE = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(str, Enum):
    DIRECTOR = "director"
    PLANNER = "planner"
    EXECUTOR = "executor"
    CHATGPT_WORK = "chatgpt_work"
    AI_SCIENTIST = "ai_scientist"


class TaskKind(str, Enum):
    WORK = "work"
    OBJECTIVE = "objective"


class ApprovalType(str, Enum):
    EXECUTE = "execute"
    COST = "cost"
    DESTRUCTIVE = "destructive"
    EXTERNAL_HANDOFF = "external_handoff"
    BUDGET_INCREASE = "budget_increase"
    LIMIT_INCREASE = "limit_increase"
    ARTIFACT_RELEASE = "artifact_release"


class ArtifactClass(str, Enum):
    REQUEST = "request"
    PLAN = "plan"
    WORK_PRODUCT = "work_product"
    EXTERNAL_HANDOFF = "external_handoff"
    SCIENTIST_OUTPUT = "scientist_output"
    EVAL_RESULT = "eval_result"
    FINAL_EVALUATION = "final_evaluation"  # held-out data: unreadable by every agent role


class TrustLevel(str, Enum):
    TRUSTED = "trusted"
    REVIEWED = "reviewed"
    UNTRUSTED = "untrusted"
    QUARANTINED = "quarantined"


ErrorClass = Literal[
    "transient", "adapter_error", "schema_violation", "timeout", "budget", "policy",
    "handoff_invalid", "crash_recovery", "hash_mismatch", "cancelled", "credit_exhausted",
]


# ---------------------------------------------------------------- contract


class ResourceLimits(StrictModel):
    max_tokens: int = Field(200_000, ge=1, le=2_000_000)
    max_usd_micro: int = Field(2_000_000, ge=0, le=50_000_000)
    wall_clock_s: int = Field(900, ge=1, le=14_400)
    container_cpu_millis: int = Field(2000, ge=250, le=4000)
    container_mem_mib: int = Field(4096, ge=256, le=16_384)
    max_retries: int = Field(2, ge=0, le=5)
    external_deadline_s: int | None = Field(None, ge=60, le=259_200)


class CheckSpec(StrictModel):
    type: Literal[
        "file_exists", "sha256_equals", "json_schema", "regex_in_artifact",
        "command_exit_zero", "metric_threshold",
    ]
    target: str = Field(min_length=1, max_length=512)
    expected: Any = None
    argv: list[str] | None = None  # never a shell string

    @model_validator(mode="after")
    def _argv_rules(self) -> CheckSpec:
        if self.type == "command_exit_zero" and not self.argv:
            raise ValueError("command_exit_zero requires argv (list, no shell)")
        if self.type != "command_exit_zero" and self.argv is not None:
            raise ValueError("argv is only valid for command_exit_zero")
        return self


class AcceptanceCriterion(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    description: str = Field(min_length=1, max_length=2000)
    mode: Literal["machine", "reviewer", "human"]
    check: CheckSpec | None = None

    @model_validator(mode="after")
    def _machine_has_check(self) -> AcceptanceCriterion:
        if self.mode == "machine" and self.check is None:
            raise ValueError("machine criteria require a check")
        return self


class ApprovalRequirement(StrictModel):
    type: ApprovalType
    reason: str = Field(min_length=1, max_length=500)


class ArtifactRef(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=SHA256_RE)


class ExpectedOutput(StrictModel):
    logical_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    media_type: str = Field(min_length=1, max_length=128)
    artifact_class: ArtifactClass
    max_bytes: int = Field(25 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)


def worst_case_total_usd_micro(limits: ResourceLimits) -> int:
    """Everything the task may cost if every permitted attempt runs to its cap: cap x (1 + max_retries)."""
    return limits.max_usd_micro * (1 + limits.max_retries)


def worst_case_total_tokens(limits: ResourceLimits) -> int:
    return limits.max_tokens * (1 + limits.max_retries)


def required_approval_types(
    assigned_role: Role, side_effect_class: SideEffectClass, max_usd_micro: int
) -> set[ApprovalType]:
    """Rule R-APPROVE (PROPOSAL 9), minimal M1 form: computed from the contract only."""
    req: set[ApprovalType] = set()
    if assigned_role in (Role.EXECUTOR, Role.AI_SCIENTIST) or side_effect_class is not SideEffectClass.NONE:
        req.add(ApprovalType.EXECUTE)
    if max_usd_micro > 0:
        req.add(ApprovalType.COST)
    if side_effect_class is SideEffectClass.DESTRUCTIVE:
        req.add(ApprovalType.DESTRUCTIVE)
    if assigned_role is Role.CHATGPT_WORK:
        req.add(ApprovalType.EXTERNAL_HANDOFF)
    if side_effect_class is SideEffectClass.EXTERNAL_WRITE:
        req.add(ApprovalType.EXTERNAL_HANDOFF)
    return req


class TaskContract(StrictModel):
    """Immutable task specification. Identity (task_id) and mutable state live on TaskRecord."""

    schema_version: Literal["task-contract/1"] = "task-contract/1"
    idempotency_key: str = Field(min_length=16, max_length=128)
    kind: TaskKind = TaskKind.WORK
    research_objective: str = Field(min_length=1, max_length=8000)
    assigned_role: Role
    input_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    expected_output: list[ExpectedOutput] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    approval_requirements: list[ApprovalRequirement] = Field(default_factory=list)
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    human_review_required: bool = False
    # Actions the executor may take. Derived ONLY from this approved contract (PROPOSAL 11.4).
    allowed_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _worst_case_is_derived(cls, data: Any) -> Any:
        """worst_case_* are DERIVED (and hashed). An inbound value must equal the derived one; it is never trusted."""
        if isinstance(data, dict) and ("worst_case_total_usd_micro" in data or "worst_case_total_tokens" in data):
            data = dict(data)
            rl = data.get("resource_limits")
            limits = rl if isinstance(rl, ResourceLimits) else ResourceLimits.model_validate(rl or {})
            for key, want in (("worst_case_total_usd_micro", worst_case_total_usd_micro(limits)),
                              ("worst_case_total_tokens", worst_case_total_tokens(limits))):
                if key in data and data.pop(key) != want:
                    raise ValueError(f"{key} does not equal per-attempt cap x (1 + max_retries) = {want}")
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst_case_total_usd_micro(self) -> int:
        return worst_case_total_usd_micro(self.resource_limits)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst_case_total_tokens(self) -> int:
        return worst_case_total_tokens(self.resource_limits)

    @field_validator("allowed_actions")
    @classmethod
    def _actions(cls, v: list[str]) -> list[str]:
        import re

        for a in v:
            if not re.fullmatch(r"[a-z0-9_.:-]{1,64}", a):
                raise ValueError(f"invalid action name {a!r}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate allowed_actions")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> TaskContract:
        ids = [c.id for c in self.acceptance_criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("acceptance criterion ids must be unique")
        declared = {r.type for r in self.approval_requirements}
        needed = required_approval_types(
            self.assigned_role, self.side_effect_class, self.resource_limits.max_usd_micro
        )
        if not needed <= declared:
            missing = sorted(t.value for t in needed - declared)
            raise ValueError(f"approval_requirements under-declared; missing {missing}")
        for c in self.acceptance_criteria:
            if (
                c.check is not None
                and c.check.type == "command_exit_zero"
                and self.assigned_role is not Role.EXECUTOR
            ):
                raise ValueError("command_exit_zero checks are only valid for the executor role")
        return self

    @property
    def side_effectful(self) -> bool:
        return self.side_effect_class in SIDE_EFFECTFUL


def contract_sha256(contract: TaskContract) -> str:
    return sha256_hex(canonical_json(contract.model_dump(mode="json")))


# ---------------------------------------------------------------- records


class ErrorRecord(StrictModel):
    ts_ms: int
    attempt: int = Field(ge=0)
    error_class: ErrorClass
    retryable: bool
    detail: str = Field(max_length=4000)


class TaskRecord(StrictModel):
    schema_version: Literal["task-record/1"] = "task-record/1"
    task_id: str
    idempotency_key: str
    kind: TaskKind
    root_task_id: str
    parent_task_id: str | None
    depth: int
    assigned_role: Role
    state: TaskState
    version: int
    contract_version: int
    blocked_reason: BlockReason | None
    blocked_from: TaskState | None
    contract: TaskContract
    contract_sha256: str
    side_effect_class: SideEffectClass
    attempt_count: int
    review_cycles: int
    timeout_retries: int
    over_root_limit: bool = False
    uncounted_attempts: int = 0   # attempts that ended in credit exhaustion do not count toward the retry limit
    review_hold: bool = False     # billing-integrity hold: only a human may review/accept
    not_before_ms: int | None
    error_history: list[ErrorRecord]
    created_by: str
    created_at_ms: int
    updated_at_ms: int
    state_entered_at_ms: int


class Run(StrictModel):
    schema_version: Literal["run/1"] = "run/1"
    run_id: str
    task_id: str
    attempt: int
    agent_id: str
    state: RunState
    lease_owner: str
    lease_expires_at_ms: int
    heartbeat_at_ms: int
    started_at_ms: int
    ended_at_ms: int | None
    error_class: str | None
    error_detail: str | None
    model: str | None = None
    cost_usd_micro: int | None = None
    cost_basis: Literal["priced", "worst_case"] | None = None
    price_table_version: str | None = None
    price_table_sha256: str | None = None


# ---------------------------------------------------------------- agent I/O


class AgentRequest(StrictModel):
    schema_version: Literal["agent-request/1"] = "agent-request/1"
    task_id: str
    run_id: str
    attempt: int = Field(ge=1)
    role: Role
    research_objective: str
    input_artifacts: list[ArtifactRef]
    expected_output: list[ExpectedOutput]
    acceptance_criteria: list[AcceptanceCriterion]
    resource_limits: ResourceLimits
    allowed_actions: list[str]
    lease_expires_at_ms: int


def build_agent_request(
    *, task_id: str, run_id: str, attempt: int, contract: TaskContract, lease_expires_at_ms: int
) -> AgentRequest:
    """The ONLY source of an agent's authority is the approved contract (untrusted-input rule).

    There is deliberately no parameter through which artifact text could influence allowed_actions.
    """
    return AgentRequest(
        task_id=task_id,
        run_id=run_id,
        attempt=attempt,
        role=contract.assigned_role,
        research_objective=contract.research_objective,
        input_artifacts=list(contract.input_artifact_refs),
        expected_output=list(contract.expected_output),
        acceptance_criteria=list(contract.acceptance_criteria),
        resource_limits=contract.resource_limits,
        allowed_actions=list(contract.allowed_actions),
        lease_expires_at_ms=lease_expires_at_ms,
    )


class ProducedArtifact(StrictModel):
    logical_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=SHA256_RE)
    size_bytes: int = Field(ge=0)
    media_type: str
    artifact_class: ArtifactClass
    parent_artifact_ids: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        parts = v.split("/")
        if v.startswith("/") or ".." in parts or "\\" in v or "\x00" in v:
            raise ValueError("artifact path must be relative with no '..'")
        return v


class ResourceUsage(StrictModel):
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    usd_micro: int = Field(ge=0)
    wall_ms: int = Field(ge=0)
    cpu_ms: int | None = Field(None, ge=0)
    peak_mem_bytes: int | None = Field(None, ge=0)
    measured_by: Literal["adapter", "container", "self_reported"]
    model: str | None = None  # the control plane prices tokens by model; adapter-reported usd_micro is ignored


class AcceptanceResult(StrictModel):
    criterion_id: str
    self_reported: Literal["pass", "fail", "not_applicable"]
    evidence_ref: str | None = None


class EvidenceCheck(StrictModel):
    check_id: str = Field(min_length=1, max_length=64)
    type: str
    target: str
    expected: Any = None
    reproduce: list[str] | None = None  # argv; verifier re-executes, no shell


class AgentResponse(StrictModel):
    schema_version: Literal["agent-response/1"] = "agent-response/1"
    task_id: str
    run_id: str
    attempt: int = Field(ge=1)
    status: Literal["succeeded", "failed", "needs_input", "refused"]
    retryable: bool = False
    produced_artifacts: list[ProducedArtifact] = Field(default_factory=list)
    resource_usage: ResourceUsage
    acceptance_results: list[AcceptanceResult] = Field(default_factory=list)
    evidence: list[EvidenceCheck] = Field(default_factory=list)
    notes: str = Field("", max_length=20_000)
    error: ErrorRecord | None = None
    # How the provider says it authenticated this run ("subscription_oauth", "manual_handoff", or something else). The control plane
    # compares it with the adapter's expected auth mode after every run (billing-integrity check, ADR-0011). None = unconfirmed.
    auth_path: str | None = None

    @model_validator(mode="after")
    def _evidence_refs_resolve(self) -> AgentResponse:
        have = {e.check_id for e in self.evidence}
        for r in self.acceptance_results:
            if r.evidence_ref is not None and r.evidence_ref not in have:
                raise ValueError(f"evidence_ref {r.evidence_ref!r} not present in evidence")
        return self


def response_problems(resp: AgentResponse, request: AgentRequest) -> list[str]:
    """Cross-checks that need the request (M1 subset of the Verifier)."""
    problems: list[str] = []
    if (resp.task_id, resp.run_id, resp.attempt) != (request.task_id, request.run_id, request.attempt):
        problems.append("response identity does not match request")
    want = sorted(c.id for c in request.acceptance_criteria)
    got = sorted(r.criterion_id for r in resp.acceptance_results)
    if want != got:
        problems.append(f"acceptance_results must cover exactly {want}, got {got}")
    return problems


# ---------------------------------------------------------------- approvals / ledger / artifacts / audit


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INVALIDATED = "invalidated"


class Approval(StrictModel):
    schema_version: Literal["approval/1"] = "approval/1"
    approval_id: str
    task_id: str
    task_version: int = Field(ge=1)  # == tasks.contract_version at request time (ADR-0009)
    contract_sha256: str = Field(pattern=SHA256_RE)
    approval_type: ApprovalType
    status: ApprovalStatus
    requested_by: str
    requested_at_ms: int
    expires_at_ms: int
    decided_by: str | None = None
    decided_at_ms: int | None = None
    reason: str | None = None
    budget_id: str | None = None  # only for budget_increase approvals
    amount_usd_micro: int | None = None
    amount_tokens: int | None = None
    worst_case_total_usd_micro: int | None = None  # what the approved contract may cost in total (cap x attempts)
    worst_case_total_tokens: int | None = None


class LedgerKind(str, Enum):
    GRANT = "grant"
    RESERVE = "reserve"
    SETTLE = "settle"
    RELEASE = "release"
    ADJUST = "adjust"
    RECONCILIATION = "reconciliation"  # human-only: aligns the estimate with the account page


class BudgetLedgerEntry(StrictModel):
    schema_version: Literal["budget-ledger-entry/1"] = "budget-ledger-entry/1"
    entry_id: str
    budget_id: str
    task_id: str | None = None
    run_id: str | None = None
    kind: LedgerKind
    usd_micro: int
    tokens: int = 0
    wall_s: int = 0
    approval_id: str | None = None
    attempt: int | None = None
    reserve_ref: str | None = None
    recorded_by: str | None = None
    flagged: bool = False
    idempotency_key: str = Field(min_length=1, max_length=200)
    created_at_ms: int

    @model_validator(mode="after")
    def _grant_needs_approval(self) -> BudgetLedgerEntry:
        if self.kind in (LedgerKind.GRANT, LedgerKind.ADJUST) and not self.approval_id:
            raise ValueError("grant/adjust entries require approval_id (human-granted)")
        return self


class Artifact(StrictModel):
    schema_version: Literal["artifact/1"] = "artifact/1"
    artifact_id: str
    sha256: str = Field(pattern=SHA256_RE)
    size_bytes: int = Field(ge=0)
    media_type: str
    artifact_class: ArtifactClass
    logical_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    storage_uri: str
    producer_run_id: str | None = None
    producer_principal_id: str | None = None
    trust_level: TrustLevel
    is_mock: bool = False
    parent_artifact_ids: list[str] = Field(default_factory=list)
    created_at_ms: int
    provenance_hash: str | None = Field(None, pattern=SHA256_RE)
    orphaned: bool = False             # its run ended timed_out/failed/cancelled/abandoned: never usable as input
    orphaned_reason: str | None = None
    removed_at_ms: int | None = None   # removal is a human-only action

    @model_validator(mode="after")
    def _one_producer(self) -> Artifact:
        if (self.producer_run_id is None) == (self.producer_principal_id is None):
            raise ValueError("exactly one of producer_run_id / producer_principal_id is required")
        return self


def provenance_hash(a: Artifact) -> str:
    return sha256_hex(
        canonical_json(
            {
                "sha256": a.sha256,
                "producer_run_id": a.producer_run_id,
                "producer_principal_id": a.producer_principal_id,
                "parent_artifact_ids": sorted(a.parent_artifact_ids),
                "created_at_ms": a.created_at_ms,
                "is_mock": a.is_mock,
            }
        )
    )


class AuditEvent(StrictModel):
    schema_version: Literal["audit-event/1"] = "audit-event/1"
    seq: int = Field(ge=1)
    event_id: str
    ts_ms: int
    task_id: str | None
    run_id: str | None
    event_type: str
    actor_id: str
    actor_kind: str
    from_state: str | None
    to_state: str | None
    payload: dict[str, Any]
    prev_hash: str = Field(pattern=SHA256_RE)
    entry_hash: str = Field(pattern=SHA256_RE)
