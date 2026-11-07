"""Planner (`claude -p`) and Director (`codex exec`) adapters behind the CliRunner seam. Subscription auth only (ADR-0011).

EVERYTHING about the CLIs' flags, output shapes and auth-reporting fields below is UNVERIFIED: it is written from memory of the
documented interfaces and exercised only against synthetic fixtures. The M4b live check (human, on the VM) records the real shapes;
until then any field we cannot confirm degrades to "unconfirmed", which sends the run to human review. Prompts go over stdin, never argv.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from airos.agents.real.cli import CliInvocation, CliResult, CliRunner
from airos.agents.real.common import (
    AdapterProtocolError,
    InputReader,
    RealAdapterBase,
    looks_like_credit_exhaustion,
)
from airos.auth import AuthMode, build_adapter_env
from airos.canonical import canonical_json
from airos.domain.schemas import AgentRequest, AgentResponse, ArtifactClass, Role, StrictModel
from airos.redaction import Redactor

# --- UNVERIFIED assumptions, centralised so the live check can correct them in one place ---------------------------------------
CLAUDE_PLANNER_ARGV = ("claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", "")  # tools disabled
CLAUDE_ACCEPTED_SUBSCRIPTION_SOURCES = frozenset({"oauth"})   # values of `apiKeySource` we accept as "subscription OAuth"
CODEX_EXEC_ARGV = ("codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "-")
CODEX_LOGIN_STATUS_ARGV = ("codex", "login", "status")
PLANNER_INSTRUCTION = ("Reply with ONE JSON object and nothing else: "
                       '{"schema_version":"plan-proposal/1","tasks":[{"title":str,"objective":str,"role":"executor|chatgpt_work|director","depends_on":[int]}]} '
                       "with at most 8 tasks. It is a PROPOSAL only.")


class ProposedTask(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=2000)
    role: Literal["executor", "chatgpt_work", "director"]
    depends_on: list[int] = Field(default_factory=list, max_length=8)


class PlanProposal(StrictModel):
    schema_version: Literal["plan-proposal/1"]
    tasks: list[ProposedTask] = Field(min_length=1, max_length=8)


def extract_claude_auth(parsed: Mapping[str, Any]) -> str | None:
    """What the CLI says its credential source was, mapped to our vocabulary; None if the output does not say."""
    src = parsed.get("apiKeySource")
    if src is None and isinstance(parsed.get("system"), dict):
        src = parsed["system"].get("apiKeySource")
    if not isinstance(src, str):
        return None
    return AuthMode.SUBSCRIPTION_OAUTH.value if src in CLAUDE_ACCEPTED_SUBSCRIPTION_SOURCES else f"unconfirmed:{src}"


class ClaudePlannerAdapter(RealAdapterBase):
    """Planner via `claude -p` with tools disabled and structured JSON output. A proposal only: it creates no tasks."""

    def __init__(self, *, model: str, credential_env: str, environ: Mapping[str, str], redactor: Redactor, runner: CliRunner) -> None:
        super().__init__(Role.PLANNER, model=model, credential_env=credential_env, environ=environ, redactor=redactor, runner=runner)

    def execute(self, request: AgentRequest, workdir: Path, inputs: InputReader | None = None) -> AgentResponse:
        runner = self._require_isolated_runner()
        token = self._credential()
        inv = CliInvocation(argv=(*CLAUDE_PLANNER_ARGV, "--model", self.model or ""),
                            env=build_adapter_env("planner", credential_value=token),
                            stdin=self._render_prompt(request, inputs) + "\n\n" + PLANNER_INSTRUCTION,
                            timeout_s=request.resource_limits.wall_clock_s)
        res = runner.run(inv)
        zero = self._usage(0, 0)
        try:
            parsed = json.loads(res.stdout) if res.stdout.strip() else {}
        except ValueError as exc:
            if res.returncode == 0:
                raise AdapterProtocolError("malformed", "CLI output is not valid JSON") from exc
            parsed = {}
        if not isinstance(parsed, dict):
            raise AdapterProtocolError("malformed", "CLI JSON is not an object")
        auth = extract_claude_auth(parsed)
        text = str(parsed.get("result", "")) if parsed else res.stderr
        if res.returncode != 0 or parsed.get("is_error") is True:
            blob = f"{text} {res.stderr}"
            if looks_like_credit_exhaustion(blob):
                return self._failed(request, zero, "credit_exhausted", "the subscription credit appears to be exhausted", False, auth)
            return self._failed(request, zero, "transient", f"claude CLI failed (exit {res.returncode})", True, auth)
        usage_obj = parsed.get("usage")
        if not isinstance(usage_obj, dict):
            raise AdapterProtocolError("malformed", "missing usage in CLI output")
        tin, tout = usage_obj.get("input_tokens"), usage_obj.get("output_tokens")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (tin, tout)):
            raise AdapterProtocolError("malformed", "usage tokens are not non-negative integers")
        usage = self._usage(int(tin), int(tout))  # type: ignore[arg-type]
        if parsed.get("subtype") == "error_max_turns" or parsed.get("stop_reason") == "max_tokens":
            raise AdapterProtocolError("truncated", "the CLI stopped before finishing")
        if not isinstance(parsed.get("result"), str):
            raise AdapterProtocolError("malformed", "missing result text")
        try:
            plan = PlanProposal.model_validate_json(parsed["result"])
        except ValidationError as exc:
            raise AdapterProtocolError("malformed", f"planner output is not a valid plan-proposal/1 ({exc.error_count()} error(s))") from exc
        produced = self._write_text_output(request, workdir, canonical_json(plan.model_dump(mode="json")), artifact_class=ArtifactClass.PLAN)
        return self._succeeded(request, produced, usage, notes="planner proposal (a proposal only; creates no tasks)", auth_path=auth)


class CodexDirectorAdapter(RealAdapterBase):
    """Director via `codex exec` with a ChatGPT login in read-only sandbox mode. Needs a confirmed ChatGPT login (no API key)."""

    def __init__(self, *, model: str, environ: Mapping[str, str], redactor: Redactor, runner: CliRunner,
                 no_api_key_attested: bool) -> None:
        super().__init__(Role.DIRECTOR, model=model, credential_env=None, environ=environ, redactor=redactor, runner=runner)
        self.no_api_key_attested = no_api_key_attested
        self.login_confirmed: bool | None = None

    def preflight(self) -> str:
        """Confirm the login mode. Returns "subscription_oauth" only for a ChatGPT login; anything else fails closed."""
        runner = self._require_isolated_runner()
        if not self.no_api_key_attested:
            raise AdapterProtocolError("billing", "the human has not attested that no API key was created in the account")
        res = runner.run(CliInvocation(argv=CODEX_LOGIN_STATUS_ARGV, env=build_adapter_env("director"), timeout_s=30))
        text = f"{res.stdout} {res.stderr}".lower()
        if res.returncode == 0 and "chatgpt" in text and "api key" not in text:
            self.login_confirmed = True
            return AuthMode.SUBSCRIPTION_OAUTH.value
        self.login_confirmed = False
        why = "an API-key login is active" if "api key" in text else "the login mode could not be confirmed as ChatGPT"
        raise AdapterProtocolError("billing", f"Director stays disabled: {why}")

    def execute(self, request: AgentRequest, workdir: Path, inputs: InputReader | None = None) -> AgentResponse:
        runner = self._require_isolated_runner()
        auth = self.preflight()  # every run re-confirms the login mode before any model call
        res: CliResult = runner.run(CliInvocation(
            argv=(*CODEX_EXEC_ARGV[:-1], "--model", self.model or "", CODEX_EXEC_ARGV[-1]), env=build_adapter_env("director"),
            stdin=self._render_prompt(request, inputs), timeout_s=request.resource_limits.wall_clock_s))
        zero = self._usage(0, 0)
        message: str | None = None
        tin = tout = 0
        saw_usage = False
        failure: str | None = None
        for raw in res.stdout.splitlines():
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except ValueError as exc:
                raise AdapterProtocolError("malformed", "codex output contains a non-JSON line") from exc
            if not isinstance(ev, dict):
                raise AdapterProtocolError("malformed", "codex event is not an object")
            t = ev.get("type")
            if t == "item.completed" and isinstance(ev.get("item"), dict) and ev["item"].get("type") == "agent_message":
                if not isinstance(ev["item"].get("text"), str):
                    raise AdapterProtocolError("malformed", "agent_message without text")
                message = ev["item"]["text"]
            elif t == "turn.completed":
                u = ev.get("usage")
                if not isinstance(u, dict) or not all(isinstance(u.get(k), int) and not isinstance(u.get(k), bool) and u[k] >= 0
                                                      for k in ("input_tokens", "output_tokens")):
                    raise AdapterProtocolError("malformed", "turn.completed without valid usage")
                tin, tout = u["input_tokens"], u["output_tokens"]
                saw_usage = True
            elif t in ("error", "turn.failed"):
                failure = str(ev.get("message") or ev.get("error") or "codex reported an error")
        if res.returncode != 0 or failure is not None:
            blob = f"{failure or ''} {res.stderr}"
            if looks_like_credit_exhaustion(blob):
                return self._failed(request, zero, "credit_exhausted", "the subscription limit appears to be reached", False, auth)
            return self._failed(request, zero, "transient", f"codex failed (exit {res.returncode})", True, auth)
        if message is None:
            raise AdapterProtocolError("malformed", "codex produced no agent_message")
        if not saw_usage:
            raise AdapterProtocolError("malformed", "codex output has no turn.completed usage")
        produced = self._write_text_output(request, workdir, message, artifact_class=ArtifactClass.PLAN)
        return self._succeeded(request, produced, self._usage(tin, tout), notes="director output (text only; no authority)", auth_path=auth)
