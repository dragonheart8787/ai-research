"""Adapter interface and the five MOCK adapters. No network, no containers, no real model calls."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from airos.domain.schemas import (
    AcceptanceResult,
    AgentRequest,
    AgentResponse,
    ArtifactClass,
    ErrorRecord,
    ProducedArtifact,
    ResourceUsage,
    Role,
)

Behavior = Literal["success", "transient_failure", "permanent_failure", "crash", "invalid_output"]

_CLASS_FOR_ROLE: dict[Role, ArtifactClass] = {
    Role.DIRECTOR: ArtifactClass.PLAN,
    Role.PLANNER: ArtifactClass.PLAN,
    Role.EXECUTOR: ArtifactClass.WORK_PRODUCT,
    Role.CHATGPT_WORK: ArtifactClass.EXTERNAL_HANDOFF,
    Role.AI_SCIENTIST: ArtifactClass.SCIENTIST_OUTPUT,
}


class AgentAdapter(Protocol):
    role: Role
    is_mock: bool

    def execute(self, request: AgentRequest, workdir: Path) -> AgentResponse:
        """Do the work inside `workdir` (outputs under workdir/out) and return a machine-readable response."""
        ...


def _always_success(_request: AgentRequest) -> Behavior:
    return "success"


@dataclass
class MockAdapter:
    """Deterministic fake agent. `script(request) -> Behavior` lets tests inject failures."""

    role: Role
    script: Callable[[AgentRequest], Behavior] = _always_success
    is_mock: bool = True

    def execute(self, request: AgentRequest, workdir: Path) -> AgentResponse:
        behavior = self.script(request)
        if behavior == "crash":
            raise RuntimeError("mock adapter crashed")
        usage = ResourceUsage(tokens_in=0, tokens_out=0, usd_micro=0, wall_ms=1, measured_by="adapter", model="mock")
        if behavior in ("transient_failure", "permanent_failure"):
            transient = behavior == "transient_failure"
            err = ErrorRecord(ts_ms=0, attempt=request.attempt, error_class="transient" if transient else "adapter_error",
                              retryable=transient, detail=f"mock {behavior}")
            return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="failed",
                                 retryable=transient, resource_usage=usage, error=err)
        out = workdir / "out"
        out.mkdir(parents=True, exist_ok=True)
        produced: list[ProducedArtifact] = []
        for eo in () if behavior == "invalid_output" else request.expected_output:   # claims success, delivers nothing
            data = f"[MOCK] {self.role.value} output for {eo.logical_name} (attempt {request.attempt})\n".encode()
            (out / eo.logical_name).write_bytes(data)
            produced.append(ProducedArtifact(
                logical_name=eo.logical_name, path=f"out/{eo.logical_name}", sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data), media_type=eo.media_type, artifact_class=_CLASS_FOR_ROLE[self.role],
                parent_artifact_ids=[a.artifact_id for a in request.input_artifacts]))
        return AgentResponse(
            task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded",
            resource_usage=usage, produced_artifacts=produced,
            acceptance_results=[AcceptanceResult(criterion_id=c.id, self_reported="pass") for c in request.acceptance_criteria],
            notes="synthetic mock output; not a research result")


def all_mock_adapters() -> dict[Role, AgentAdapter]:
    return {r: MockAdapter(r) for r in Role}
