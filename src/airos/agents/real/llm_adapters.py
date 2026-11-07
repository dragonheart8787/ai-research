"""OpenAI Director and Anthropic Planner adapters (chat-style HTTP APIs behind the Transport seam).

Wire formats follow the providers' documented request/response shapes. They are verified ONLY against fixtures in M3
(see tests/fixtures/README.md); exact parameter names must be confirmed by a live contract run in M4/M5.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from airos.agents.real.common import (
    AdapterProtocolError,
    InputReader,
    RealAdapterBase,
    expect_int,
    expect_str,
)
from airos.agents.real.transport import Transport, TransportError
from airos.domain.schemas import AgentRequest, AgentResponse, ArtifactClass, Role
from airos.redaction import Redactor

DIRECTOR_SYSTEM = ("You are the Research Director of an orchestration system. Produce the requested brief or review as plain "
                   "text. You cannot approve, cancel, spend or change anything; you only write text.")
PLANNER_SYSTEM = ("You are the Planner of an orchestration system. Produce a decomposition proposal as plain text/JSON. "
                  "It is a PROPOSAL: a human and the control plane decide what becomes a task.")


class OpenAIDirectorAdapter(RealAdapterBase):
    URL = "https://api.openai.com/v1/chat/completions"
    HOSTS = frozenset({"api.openai.com"})

    def __init__(self, *, model: str, credential_env: str, environ: Mapping[str, str], redactor: Redactor, transport: Transport) -> None:
        super().__init__(Role.DIRECTOR, model=model, credential_env=credential_env, environ=environ, redactor=redactor,
                         transport=transport, allowed_hosts=self.HOSTS)

    def execute(self, request: AgentRequest, workdir: Path, inputs: InputReader | None = None) -> AgentResponse:
        key = self._credential()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": DIRECTOR_SYSTEM},
                                {"role": "user", "content": self._render_prompt(request, inputs)}],
                   "max_tokens": min(request.resource_limits.max_tokens, 8192)}
        try:
            res = self._post_json(self.URL, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload,
                                  request.resource_limits.wall_clock_s)
        except TransportError as exc:
            return self._failed(request, self._usage(0, 0), "transient", f"transport: {exc}", True)
        data = res.data
        usage = self._usage(0, 0)
        fail = self._http_failure(request, res, usage)
        if fail is not None:
            return fail
        usage = self._usage(expect_int(data, "usage", "prompt_tokens"), expect_int(data, "usage", "completion_tokens"))
        choice_finish = expect_str(data, "choices", 0, "finish_reason")
        msg = data["choices"][0].get("message") if isinstance(data["choices"][0], dict) else None
        if not isinstance(msg, dict):
            raise AdapterProtocolError("malformed", "choices[0].message is missing")
        if msg.get("refusal"):
            return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="refused",
                                 resource_usage=usage, notes=self.redactor.redact_text(str(msg["refusal"]))[:500])
        if choice_finish == "length":
            raise AdapterProtocolError("truncated", "the model stopped at the token limit; output is incomplete")
        content = expect_str(msg, "content")
        produced = self._write_text_output(request, workdir, content, artifact_class=ArtifactClass.PLAN)
        return self._succeeded(request, produced, usage, notes="director output (text only; no authority)")


class AnthropicPlannerAdapter(RealAdapterBase):
    URL = "https://api.anthropic.com/v1/messages"
    HOSTS = frozenset({"api.anthropic.com"})
    VERSION = "2023-06-01"

    def __init__(self, *, model: str, credential_env: str, environ: Mapping[str, str], redactor: Redactor, transport: Transport) -> None:
        super().__init__(Role.PLANNER, model=model, credential_env=credential_env, environ=environ, redactor=redactor,
                         transport=transport, allowed_hosts=self.HOSTS)

    def execute(self, request: AgentRequest, workdir: Path, inputs: InputReader | None = None) -> AgentResponse:
        key = self._credential()
        payload = {"model": self.model, "max_tokens": min(request.resource_limits.max_tokens, 8192), "system": PLANNER_SYSTEM,
                   "messages": [{"role": "user", "content": self._render_prompt(request, inputs)}]}
        try:
            res = self._post_json(self.URL, {"x-api-key": key, "anthropic-version": self.VERSION, "content-type": "application/json"},
                                  payload, request.resource_limits.wall_clock_s)
        except TransportError as exc:
            return self._failed(request, self._usage(0, 0), "transient", f"transport: {exc}", True)
        data = res.data
        fail = self._http_failure(request, res, self._usage(0, 0))
        if fail is not None:
            return fail
        usage = self._usage(expect_int(data, "usage", "input_tokens"), expect_int(data, "usage", "output_tokens"))
        if expect_str(data, "type") != "message":
            raise AdapterProtocolError("malformed", "response type is not 'message'")
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise AdapterProtocolError("malformed", "content is missing or empty")
        texts = []
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str):
                raise AdapterProtocolError("malformed", "unsupported content block (only text is accepted)")
            texts.append(b["text"])
        stop = data.get("stop_reason")
        if stop == "max_tokens":
            raise AdapterProtocolError("truncated", "the model stopped at max_tokens; output is incomplete")
        if stop == "refusal":
            return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="refused",
                                 resource_usage=usage, notes="model refused")
        produced = self._write_text_output(request, workdir, "\n".join(texts), artifact_class=ArtifactClass.PLAN)
        return self._succeeded(request, produced, usage, notes="planner proposal (a proposal only; creates no tasks)")
