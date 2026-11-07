"""Loads config/agents.toml (no secrets) into validated, immutable objects. Fails closed: anything unexpected is a ConfigError."""
from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from airos.agents.registry import AgentSpec, RegistryPolicy, model_problem
from airos.auth import AuthMode, EnvPolicyViolation, validate_credential_host_var, validate_requested_env
from airos.domain.limits import Limits
from airos.errors import AirosError
from airos.redaction import Redactor

ROLES = ("director", "planner", "executor", "chatgpt_work", "ai_scientist")
USD_MICRO = 1_000_000


class ConfigError(AirosError):
    pass


@dataclass(frozen=True)
class AgentsConfig:
    policy: RegistryPolicy
    specs: tuple[AgentSpec, ...]
    limits: Limits
    quota_probe_enabled: bool = False
    raw_roles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _only(d: Mapping[str, Any], allowed: set[str], where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"unknown key(s) {sorted(extra)} in [{where}]")


def load_agents_config(source: str | Path, *, today: date | None = None, base_limits: Limits | None = None) -> AgentsConfig:
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    _only(data, {"security", "models", "roles", "budget", "quota_probe"}, "root")
    red = Redactor()
    for line in text.splitlines():  # a config file must never carry a secret, and has no API-key setting of any kind
        code = line.split("#")[0]
        if red.contains_secret(line):
            raise ConfigError("config contains a secret-looking value; credentials belong in the environment, referenced by name")
        if any(w in code.lower().replace("no_api_key_attested", "") for w in ("api_key", "apikey", "api-key")):
            raise ConfigError("API-key settings do not exist: this system has no API-key mode (ADR-0011)")

    sec = data.get("security", {})
    _only(sec, {"usage_credits_attested_off", "attested_on"}, "security")
    attested = sec.get("usage_credits_attested_off", False)
    if not isinstance(attested, bool):
        raise ConfigError("usage_credits_attested_off must be a boolean")
    on_raw = sec.get("attested_on", "")
    try:
        attested_on = date.fromisoformat(on_raw) if on_raw else None
    except ValueError as exc:
        raise ConfigError(f"attested_on {on_raw!r} is not an ISO date") from exc
    models = data.get("models", {})
    _only(models, {"planner", "executor", "director"}, "models")
    allow = {k: frozenset(models.get(k, [])) for k in ("planner", "executor")}
    from airos.agents.registry import DEFAULT_MODEL_ALLOWLIST

    for k in ("planner", "executor"):
        if allow[k] != DEFAULT_MODEL_ALLOWLIST[k]:
            raise ConfigError(f"[models].{k} must be exactly {sorted(DEFAULT_MODEL_ALLOWLIST[k])}; other models fail closed")
    policy = RegistryPolicy(usage_credits_attested_off=attested, attested_on=attested_on, model_allowlist=allow,
                            **({"today": (lambda t=today: t)} if today else {}))

    specs: list[AgentSpec] = []
    roles = data.get("roles", {})
    _only(roles, set(ROLES), "roles")
    for role in ROLES:
        r = roles.get(role, {})
        _only(r, {"enabled", "auth_mode", "model", "credential_env", "no_api_key_attested", "login_volume", "repo_path",
                  "allowed_authors", "require_signature", "pass_env"}, f"roles.{role}")
        mode = r.get("auth_mode", "manual_handoff" if role == "chatgpt_work" else "subscription_oauth")
        try:
            AuthMode(mode)
        except ValueError as exc:
            raise ConfigError(f"roles.{role}.auth_mode {mode!r} does not exist (only subscription_oauth / manual_handoff)") from exc
        model = r.get("model", "") or None
        if model and "fable" in model.lower():
            raise ConfigError(f"roles.{role}.model {model!r}: Fable models are refused")
        if role in ("planner", "executor") and model is not None:
            problem = model_problem(role, model, policy)
            if problem:
                raise ConfigError(f"roles.{role}: {problem}")
        cred = r.get("credential_env") or None
        pass_env = [x.strip() for x in str(r.get("pass_env", "")).split(",") if x.strip()]
        try:
            validate_credential_host_var(cred)
            validate_requested_env(role, pass_env)
        except EnvPolicyViolation as exc:
            raise ConfigError(str(exc)) from exc
        opts: dict[str, str] = {}
        if role == "director":
            opts["no_api_key_attested"] = "1" if r.get("no_api_key_attested") is True else "0"
            if r.get("login_volume"):
                opts["login_volume"] = str(r["login_volume"])
        if role == "chatgpt_work":
            if r.get("repo_path"):
                opts["repo_path"] = str(r["repo_path"])
            if r.get("allowed_authors"):
                opts["allowed_authors"] = str(r["allowed_authors"])
            if r.get("require_signature") is True:
                opts["require_signature"] = "1"
        if pass_env:
            opts["pass_env"] = ",".join(pass_env)
        enabled = r.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"roles.{role}.enabled must be a boolean")
        specs.append(AgentSpec(role=role, kind="real", enabled=enabled, credential_env=cred, model=model, options=opts,
                               auth_mode=str(mode)))

    b = data.get("budget", {})
    _only(b, {"agent_sdk_credit_monthly_cap_usd", "unallocated_usd", "allocations_usd", "director"}, "budget")
    cap = b.get("agent_sdk_credit_monthly_cap_usd", 15)
    alloc_raw = b.get("allocations_usd", {"executor": 10, "planner": 3, "ai_scientist": 0})
    _only(alloc_raw, {"executor", "planner", "ai_scientist"}, "budget.allocations_usd")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in [cap, *alloc_raw.values()]):
        raise ConfigError("budget amounts must be non-negative numbers (USD)")
    alloc = {k: int(round(v * USD_MICRO)) for k, v in alloc_raw.items()}
    cap_micro = int(round(cap * USD_MICRO))
    if sum(alloc.values()) > cap_micro:
        raise ConfigError("sum of allocations exceeds the monthly cap")
    unalloc = b.get("unallocated_usd", (cap_micro - sum(alloc.values())) / USD_MICRO)
    if int(round(unalloc * USD_MICRO)) != cap_micro - sum(alloc.values()):
        raise ConfigError("unallocated_usd must equal cap - sum(allocations)")
    director = b.get("director", {})
    _only(director, {"max_runs_per_day"}, "budget.director")
    runs = director.get("max_runs_per_day", 0)
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 0:
        raise ConfigError("budget.director.max_runs_per_day must be a non-negative integer")
    base = base_limits or Limits()
    limits = Limits(**{**base.__dict__, "agent_sdk_credit_monthly_cap_usd_micro": cap_micro,
                       "role_allocations_usd_micro": alloc, "director_max_runs_per_day": runs})
    qp = data.get("quota_probe", {})
    _only(qp, {"enabled"}, "quota_probe")
    return AgentsConfig(policy=policy, specs=tuple(specs), limits=limits, quota_probe_enabled=qp.get("enabled", False) is True,
                        raw_roles=roles)
