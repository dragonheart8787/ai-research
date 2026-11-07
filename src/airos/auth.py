"""Authentication model (ADR-0011): subscription OAuth or manual handoff ONLY. There is no api_key mode and no code path that
reads, builds or passes a provider API key. Adapter subprocesses/containers get an explicit environment built from an allowlist;
nothing is inherited from the host shell.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum

from airos.errors import AirosError


class AuthMode(str, Enum):
    SUBSCRIPTION_OAUTH = "subscription_oauth"
    MANUAL_HANDOFF = "manual_handoff"
    # deliberately no API_KEY member: `AuthMode("api_key")` raises ValueError


class EnvPolicyViolation(AirosError):
    """A config or caller asked for an environment variable that must never reach an adapter."""

    def __init__(self, name: str, why: str) -> None:
        super().__init__(f"environment variable {name!r} refused: {why}")
        self.name, self.why = name, why


FORBIDDEN_ENV_EXACT = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY",
    # AWS Bedrock (and any AWS credential): never passed
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
})
FORBIDDEN_ENV_PREFIXES = ("AWS_", "ANTHROPIC_", "OPENAI_", "CODEX_API", "GEMINI_", "AZURE_OPENAI", "GOOGLE_APPLICATION_CREDENTIALS")
FORBIDDEN_ENV_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN", "_SECRET_ACCESS_KEY")

# The only variable names that may carry credential material into an adapter, and the role that may receive each.
CLAUDE_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_ROLES = frozenset({"planner", "executor"})
CODEX_HOME_VAR = "CODEX_HOME"
CODEX_HOME_IN_CONTAINER = "/codex-home"
DEFAULT_HOME = "/tmp/airos-home"  # noqa: S108 - a path INSIDE the sandbox, never on the host

BASE_ENV: Mapping[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",  # fixed; the host PATH is never inherited
    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
}
# Extra, non-secret names a config may ask to pass through (per role). Empty by default: asking for anything else is refused.
PASS_THROUGH_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "planner": frozenset(), "executor": frozenset(), "director": frozenset(), "chatgpt_work": frozenset(), "ai_scientist": frozenset(),
}


def is_forbidden_env_name(name: str) -> bool:
    n = name.upper()
    return n in FORBIDDEN_ENV_EXACT or n.startswith(FORBIDDEN_ENV_PREFIXES) or n.endswith(FORBIDDEN_ENV_SUFFIXES)


def validate_requested_env(role: str, names: Iterable[str]) -> list[str]:
    """Return the accepted names or raise EnvPolicyViolation on the FIRST forbidden / non-allowlisted one."""
    ok: list[str] = []
    for name in names:
        if is_forbidden_env_name(name):
            raise EnvPolicyViolation(name, "provider API keys / billing variables are never passed to adapters")
        if name not in PASS_THROUGH_ALLOWLIST.get(role, frozenset()):
            raise EnvPolicyViolation(name, f"not on the pass-through allowlist for role {role!r}")
        ok.append(name)
    return ok


def validate_credential_host_var(name: str | None) -> None:
    """The HOST variable that holds a role's subscription token must not itself be a forbidden (billing) name."""
    if name and is_forbidden_env_name(name):
        raise EnvPolicyViolation(name, "a credential may not be sourced from a provider API-key variable")


def build_adapter_env(role: str, *, credential_value: str | None = None, home: str = DEFAULT_HOME) -> dict[str, str]:
    """The COMPLETE environment of an adapter process/container. Built from constants plus at most one credential:
    Claude roles get CLAUDE_CODE_OAUTH_TOKEN; the Director gets only CODEX_HOME (login state lives in its own volume)."""
    env = dict(BASE_ENV)
    env["HOME"] = home
    if role in CLAUDE_ROLES and credential_value:
        env[CLAUDE_TOKEN_VAR] = credential_value
    if role == "director":
        env[CODEX_HOME_VAR] = CODEX_HOME_IN_CONTAINER
    bad = [k for k in env if is_forbidden_env_name(k)]
    if bad:  # defence in depth: can only happen if the constants above are edited wrongly
        raise EnvPolicyViolation(bad[0], "constructed environment contains a forbidden variable")
    return env


def scan_host_environment(host_environ: Mapping[str, str]) -> list[str]:
    """Names (never values) of forbidden variables present in the host environment. They are ignored, not inherited."""
    return sorted(k for k in host_environ if is_forbidden_env_name(k))
