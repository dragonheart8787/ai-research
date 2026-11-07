"""Agent registry and routing. FAILS CLOSED: unknown role, disabled adapter, missing credentials, a model without a price,
or a real adapter that cannot be built never yields an adapter; every refusal is audited by `route()`.

Gate order for a real adapter: enabled -> credentials (if the role needs any) -> price-table entry (if it uses a model)
-> implementation/configuration present -> built lazily and cached. Mock adapters only ever come from the mock table."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from airos.agents.adapters import AgentAdapter
from airos.auth import AuthMode, EnvPolicyViolation, validate_credential_host_var, validate_requested_env
from airos.domain.schemas import Role, TaskRecord
from airos.errors import AdapterUnavailable
from airos.persistence.sqlite_repo import SqliteRepository
from airos.pricing import PriceTable
from airos.redaction import Redactor


@dataclass(frozen=True)
class AgentSpec:
    role: str
    kind: Literal["mock", "real"] = "mock"
    enabled: bool = True
    credential_env: str | None = None  # name of the env var holding the credential (never the value)
    model: str | None = None  # real adapters that call a model: must have an entry in the pinned price table
    options: Mapping[str, str] = field(default_factory=dict)  # non-secret settings (e.g. handoff repo path, pass_env)
    auth_mode: str = "subscription_oauth"  # subscription_oauth | manual_handoff; "api_key" does not exist and is refused


# Roles that authenticate with a Claude subscription: they refuse to start without the human's usage-credits-OFF attestation.
CLAUDE_DRIVEN_ROLES = frozenset({"planner", "executor"})
EXPECTED_AUTH_MODE = {"planner": "subscription_oauth", "executor": "subscription_oauth", "director": "subscription_oauth",
                      "ai_scientist": "subscription_oauth", "chatgpt_work": "manual_handoff"}
# Claude model allowlist. Anything else (including every Fable model) fails closed. The Director's model is set by the human.
DEFAULT_MODEL_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "planner": frozenset({"claude-opus-5-5"}), "executor": frozenset({"claude-sonnet-5-5"}),
}


@dataclass(frozen=True)
class RegistryPolicy:
    """Human-set safety switches (config/agents.toml). The system CANNOT verify usage_credits_attested_off: it only refuses to start
    Claude-driven adapters until a human has asserted it, with a date."""

    usage_credits_attested_off: bool = False
    attested_on: date | None = None
    model_allowlist: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(DEFAULT_MODEL_ALLOWLIST))
    today: Callable[[], date] = lambda: datetime.now(UTC).date()

    def attestation_problem(self) -> str | None:
        if not self.usage_credits_attested_off:
            return "usage_credits_attested_off is false: the human has not attested that provider 'usage credits' (overflow billing) are OFF"
        if self.attested_on is None:
            return "usage_credits_attested_off is true but attested_on is missing"
        if self.attested_on > self.today():
            return "attested_on is in the future"
        return None


def model_problem(role: str, model: str | None, policy: RegistryPolicy) -> str | None:
    if not model:
        return "no model configured"
    if "fable" in model.lower():
        return f"model {model!r} is not allowed (Fable models are refused)"
    allowed = policy.model_allowlist.get(role)
    if allowed is not None and model not in allowed:
        return f"model {model!r} is not on the allowlist for {role} ({sorted(allowed)})"
    return None


@dataclass(frozen=True)
class RealFactory:
    requires_credential: bool
    requires_model: bool
    build: Callable[[AgentSpec, Mapping[str, str], Redactor], AgentAdapter]


class AgentRegistry:
    def __init__(self, specs: list[AgentSpec], adapters: Mapping[Role, AgentAdapter], environ: Mapping[str, str],
                 price_table: PriceTable | None = None, real_factories: Mapping[Role, RealFactory] | None = None,
                 redactor: Redactor | None = None, policy: RegistryPolicy | None = None) -> None:
        self._policy = policy or RegistryPolicy()
        self._prices = price_table or PriceTable.default()
        self._specs = {s.role: s for s in specs}
        self._adapters = dict(adapters)
        self._environ = environ
        self._factories = dict(real_factories or {})
        self._redactor = redactor or Redactor()
        self._built: dict[str, AgentAdapter] = {}

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
            return self._resolve_real(r, spec)
        adapter = self._adapters.get(r)
        if adapter is None or not adapter.is_mock:
            raise AdapterUnavailable(f"no mock adapter registered for {role!r}")
        return adapter

    def _resolve_real(self, r: Role, spec: AgentSpec) -> AgentAdapter:
        role = r.value
        factory = self._factories.get(r)
        needs_cred = True if factory is None else factory.requires_credential
        needs_model = True if factory is None else factory.requires_model
        # --- authentication model (ADR-0011): subscription or manual handoff only; never an API key
        try:
            mode = AuthMode(spec.auth_mode)
        except ValueError as exc:
            raise AdapterUnavailable(f"auth_mode {spec.auth_mode!r} does not exist (only subscription_oauth / manual_handoff)") from exc
        if mode.value != EXPECTED_AUTH_MODE[role]:
            raise AdapterUnavailable(f"auth_mode {mode.value!r} is not valid for {role!r} (expected {EXPECTED_AUTH_MODE[role]!r})")
        try:
            validate_credential_host_var(spec.credential_env)
            validate_requested_env(role, [x for x in spec.options.get("pass_env", "").split(",") if x.strip()])
        except EnvPolicyViolation as exc:
            raise AdapterUnavailable(f"refused: {exc}") from exc
        if needs_model:
            problem = model_problem(role, spec.model, self._policy)
            if problem:
                raise AdapterUnavailable(f"real adapter for {role!r}: {problem}")
        if role in CLAUDE_DRIVEN_ROLES:
            problem = self._policy.attestation_problem()
            if problem:
                raise AdapterUnavailable(f"real adapter for {role!r} refuses to start: {problem}")
        if role == "director" and spec.options.get("no_api_key_attested") != "1":
            raise AdapterUnavailable("real adapter for 'director' refuses to start: the human has not attested that no API key was created "
                                     "in the ChatGPT/OpenAI account (the Director stays disabled)")
        if needs_cred and (not spec.credential_env or not self._environ.get(spec.credential_env)):
            raise AdapterUnavailable(f"real adapter for {role!r} has no configured credentials")
        if needs_model and not self._prices.has(spec.model):
            raise AdapterUnavailable(
                f"real adapter for {role!r}: model {spec.model!r} has no entry in price table {self._prices.version}")
        if factory is None:
            raise AdapterUnavailable(f"no real adapter implementation exists for {role!r} in this build")
        if role in self._built:
            return self._built[role]
        try:
            adapter = factory.build(spec, self._environ, self._redactor)
        except AdapterUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a builder that fails is a refusal, never a half-built adapter
            raise AdapterUnavailable(f"real adapter for {role!r} could not be built: {type(exc).__name__}: {exc}") from exc
        if adapter.is_mock:
            raise AdapterUnavailable(f"factory for {role!r} returned a mock adapter")
        self._built[role] = adapter
        return adapter

    def route(self, repo: SqliteRepository, task: TaskRecord) -> AgentAdapter:
        """Resolve the adapter for `task`; on refusal write an audited error and raise AdapterUnavailable."""
        try:
            return self.resolve(task.assigned_role.value)
        except AdapterUnavailable as exc:
            repo.record_system_event("agent.route_failed", {"role": task.assigned_role.value, "reason": str(exc)}, task.task_id)
            if "refused:" in str(exc) and "environment variable" in str(exc):
                repo.record_system_event("env.forbidden_refused", {"role": task.assigned_role.value, "reason": str(exc)}, task.task_id)
            raise
