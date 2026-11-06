"""Agent registry and routing. FAILS CLOSED: unknown role, disabled adapter, or a real adapter without
configured credentials (or without an implementation) never yields an adapter; every refusal is audited."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from airos.agents.adapters import AgentAdapter
from airos.domain.schemas import Role, TaskRecord
from airos.errors import AdapterUnavailable
from airos.persistence.sqlite_repo import SqliteRepository


@dataclass(frozen=True)
class AgentSpec:
    role: str
    kind: Literal["mock", "real"] = "mock"
    enabled: bool = True
    credential_env: str | None = None  # name of the env var holding the credential (never the value)


class AgentRegistry:
    def __init__(self, specs: list[AgentSpec], adapters: Mapping[Role, AgentAdapter], environ: Mapping[str, str]) -> None:
        self._specs = {s.role: s for s in specs}
        self._adapters = dict(adapters)
        self._environ = environ

    def real_roles(self) -> frozenset[str]:
        return frozenset(r for r, s in self._specs.items() if s.kind == "real")

    def resolve(self, role: str) -> AgentAdapter:
        spec = self._specs.get(role)
        if spec is None:
            raise AdapterUnavailable(f"unknown role {role!r}: no agent spec registered")
        try:
            r = Role(role)
        except ValueError as exc:
            raise AdapterUnavailable(f"unknown role {role!r}") from exc
        if not spec.enabled:
            raise AdapterUnavailable(f"adapter for {role!r} is disabled")
        if spec.kind == "real":
            if not spec.credential_env or not self._environ.get(spec.credential_env):
                raise AdapterUnavailable(f"real adapter for {role!r} has no configured credentials")
            raise AdapterUnavailable(f"no real adapter implementation exists for {role!r} in this build (mock-only)")
        adapter = self._adapters.get(r)
        if adapter is None or not adapter.is_mock:
            raise AdapterUnavailable(f"no mock adapter registered for {role!r}")
        return adapter

    def route(self, repo: SqliteRepository, task: TaskRecord) -> AgentAdapter:
        """Resolve the adapter for `task`; on refusal write an audited error and raise AdapterUnavailable."""
        try:
            return self.resolve(task.assigned_role.value)
        except AdapterUnavailable as exc:
            repo.record_system_event("agent.route_failed", {"role": task.assigned_role.value, "reason": str(exc)}, task.task_id)
            raise
