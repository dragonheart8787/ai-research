"""Factories that turn an AgentSpec into a real adapter. Nothing runs unless the deployment passes a runner/loop/handoff configuration in;
with none, every real role fails closed at routing time. Subscription auth only (ADR-0011)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from airos.agents.adapters import AgentAdapter
from airos.agents.real.claude_executor import AgentLoop, ClaudeAgentSdkExecutorAdapter
from airos.agents.real.cli import CliRunner
from airos.agents.real.cli_adapters import ClaudePlannerAdapter, CodexDirectorAdapter
from airos.agents.real.handoff import ChatGPTWorkHandoffAdapter, GitHandoff, HandoffConfig, HandoffConfigError
from airos.agents.registry import AgentSpec, RealFactory
from airos.domain.schemas import Role
from airos.errors import AdapterUnavailable
from airos.redaction import Redactor

# Default HOST variable NAMES that hold each Claude adapter's own subscription token (values are never in the repo).
# The token is injected into that adapter's container as CLAUDE_CODE_OAUTH_TOKEN and nowhere else.
DEFAULT_CREDENTIAL_ENV = {
    Role.PLANNER: "AIROS_CLAUDE_TOKEN_PLANNER",
    Role.EXECUTOR: "AIROS_CLAUDE_TOKEN_EXECUTOR",
}

SCIENTIST_STUB_REASON = ("AI Scientist v3 is a fail-closed stub: its isolation boundary (separate container, network, volumes, "
                         "credentials, budget; ADR-0005) is M5 and is not implemented, so it can never be enabled")


def _scientist(_spec: AgentSpec, _env: Mapping[str, str], _red: Redactor) -> AgentAdapter:
    raise AdapterUnavailable(SCIENTIST_STUB_REASON)


def real_factories(*, cli_runner: CliRunner | None = None, executor_loop: AgentLoop | None = None,
                   handoff: HandoffConfig | None = None) -> dict[Role, RealFactory]:
    def director(spec: AgentSpec, env: Mapping[str, str], red: Redactor) -> AgentAdapter:
        if cli_runner is None:
            raise AdapterUnavailable("no sandboxed CLI runner configured")
        return CodexDirectorAdapter(model=spec.model or "", environ=env, redactor=red, runner=cli_runner,
                                    no_api_key_attested=spec.options.get("no_api_key_attested") == "1")

    def planner(spec: AgentSpec, env: Mapping[str, str], red: Redactor) -> AgentAdapter:
        if cli_runner is None:
            raise AdapterUnavailable("no sandboxed CLI runner configured")
        return ClaudePlannerAdapter(model=spec.model or "", credential_env=spec.credential_env or "", environ=env, redactor=red,
                                    runner=cli_runner)

    def executor(spec: AgentSpec, env: Mapping[str, str], red: Redactor) -> AgentAdapter:
        if executor_loop is None:
            raise AdapterUnavailable("Claude Agent SDK loop is not configured (it runs inside the sandbox; M4b)")
        return ClaudeAgentSdkExecutorAdapter(model=spec.model or "", credential_env=spec.credential_env or "", environ=env,
                                             redactor=red, loop=executor_loop)

    def chatgpt(spec: AgentSpec, env: Mapping[str, str], red: Redactor) -> AgentAdapter:
        if handoff is None and "repo_path" not in spec.options:
            raise AdapterUnavailable("no handoff repository configured")
        cfg = handoff
        if cfg is None:
            authors = frozenset(a.strip().lower() for a in spec.options.get("allowed_authors", "").split(",") if a.strip())
            cfg = HandoffConfig(repo_path=Path(spec.options["repo_path"]), allowed_authors=authors,
                                work_root=Path(spec.options.get("work_root", "")),
                                require_signature=spec.options.get("require_signature") == "1")
        try:
            return ChatGPTWorkHandoffAdapter(git=GitHandoff(cfg), redactor=red)
        except HandoffConfigError as exc:
            raise AdapterUnavailable(f"handoff misconfigured: {exc}") from exc

    # (requires_credential, requires_model): the Director has no token (login state lives in its own volume) but does use a model.
    return {
        Role.DIRECTOR: RealFactory(False, True, director),
        Role.PLANNER: RealFactory(True, True, planner),
        Role.EXECUTOR: RealFactory(True, True, executor),
        Role.CHATGPT_WORK: RealFactory(False, False, chatgpt),
        Role.AI_SCIENTIST: RealFactory(True, True, _scientist),
    }
