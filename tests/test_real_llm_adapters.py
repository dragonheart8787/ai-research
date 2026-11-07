"""OpenAI Director and Anthropic Planner adapters, driven ONLY by synthetic fixtures (see tests/fixtures/README.md).
Contract tests: exact request shape; strict, fail-closed response parsing."""
from __future__ import annotations

import json

import pytest

from airos.agents.real.common import MAX_HTTP_BODY_BYTES, AdapterProtocolError
from airos.agents.real.llm_adapters import AnthropicPlannerAdapter, OpenAIDirectorAdapter
from airos.agents.real.transport import (
    FixtureMismatch,
    FixtureTransport,
    HttpRequest,
    HttpResponse,
    TransportError,
)
from airos.domain.schemas import ArtifactClass, ExpectedOutput, Role
from airos.redaction import Redactor
from tests.real_helpers import (
    ANTHROPIC_PLANNER_ENV,
    ENV,
    KEY_ANTHROPIC,
    KEY_OPENAI,
    OPENAI_ENV,
    anthropic_fx,
    openai_fx,
    req,
)


def _director(fixtures, env=None, red=None):
    t = FixtureTransport(fixtures)
    return OpenAIDirectorAdapter(model="gpt-test", credential_env=OPENAI_ENV, environ=ENV if env is None else env,
                                 redactor=red or Redactor(), transport=t), t


def _planner(fixtures, env=None, red=None):
    t = FixtureTransport(fixtures)
    return AnthropicPlannerAdapter(model="claude-test", credential_env=ANTHROPIC_PLANNER_ENV, environ=ENV if env is None else env,
                                   redactor=red or Redactor(), transport=t), t


# ------------------------------------------------------------------ request contract


def test_openai_request_has_exactly_the_documented_shape(tmp_path):
    a, t = _director([openai_fx()])
    a.execute(req(Role.DIRECTOR), tmp_path)
    [call] = t.calls
    body = json.loads(call.body)
    assert (call.method, call.url) == ("POST", "https://api.openai.com/v1/chat/completions")
    assert set(body) == {"model", "messages", "max_tokens"} and body["model"] == "gpt-test"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert call.headers["Authorization"] == f"Bearer {KEY_OPENAI}" and call.headers["Content-Type"] == "application/json"
    assert 0 < body["max_tokens"] <= 8192


def test_anthropic_request_has_exactly_the_documented_shape(tmp_path):
    a, t = _planner([anthropic_fx()])
    a.execute(req(Role.PLANNER), tmp_path)
    [call] = t.calls
    body = json.loads(call.body)
    assert (call.method, call.url) == ("POST", "https://api.anthropic.com/v1/messages")
    assert set(body) == {"model", "max_tokens", "system", "messages"} and body["messages"][0]["role"] == "user"
    assert call.headers["x-api-key"] == KEY_ANTHROPIC and call.headers["anthropic-version"] == "2023-06-01"


def test_credential_is_only_in_the_header_never_in_body_prompt_or_repr(tmp_path):
    a, t = _director([openai_fx()])
    a.execute(req(Role.DIRECTOR), tmp_path)
    [call] = t.calls
    assert KEY_OPENAI.encode() not in call.body
    assert KEY_OPENAI not in repr(call) and "***" in repr(call)


def test_prompt_comes_from_the_contract_objective_and_marks_inputs_untrusted(tmp_path):
    a, t = _director([openai_fx()])
    a.execute(req(Role.DIRECTOR, objective="OBJECTIVE-FROM-CONTRACT"), tmp_path)
    prompt = json.loads(t.calls[0].body)["messages"][1]["content"]
    assert "OBJECTIVE-FROM-CONTRACT" in prompt and "UNTRUSTED DATA" in prompt


# ------------------------------------------------------------------ success paths


def test_openai_success_writes_redacted_bounded_output_and_reports_tokens(tmp_path):
    a, _ = _director([openai_fx(content="Brief text", usage=(120, 30))])
    r = a.execute(req(Role.DIRECTOR), tmp_path)
    assert r.status == "succeeded" and not a.is_mock
    assert (r.resource_usage.tokens_in, r.resource_usage.tokens_out, r.resource_usage.model) == (120, 30, "gpt-test")
    assert r.resource_usage.usd_micro == 0                                  # USD is the control plane's job, never the adapter's
    [p] = r.produced_artifacts
    assert (tmp_path / p.path).read_text() == "Brief text" and p.artifact_class is ArtifactClass.PLAN


def test_anthropic_success_joins_text_blocks(tmp_path):
    a, _ = _planner([anthropic_fx(blocks=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], usage=(7, 3))])
    r = a.execute(req(Role.PLANNER), tmp_path)
    assert r.status == "succeeded" and (tmp_path / r.produced_artifacts[0].path).read_text() == "a\nb"
    assert (r.resource_usage.tokens_in, r.resource_usage.tokens_out) == (7, 3)


# ------------------------------------------------------------------ provider errors: retry policy by status


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (400, False), (404, False), (408, True), (429, True),
                                              (500, True), (502, True), (503, True)])
def test_http_errors_map_to_retryable_or_final(tmp_path, status, retryable):
    for make, fx in ((_director, openai_fx(status=status, raw="{}")), (_planner, anthropic_fx(status=status, raw="{}"))):
        a, _ = make([fx])
        r = a.execute(req(a.role), tmp_path)
        assert r.status == "failed" and r.retryable is retryable and r.error is not None and r.error.retryable is retryable
        assert not r.produced_artifacts


def test_transport_failure_is_a_retryable_failed_response_not_a_crash(tmp_path):
    class Down:
        def send(self, request):
            raise TransportError("connection reset")

    a = OpenAIDirectorAdapter(model="gpt-test", credential_env=OPENAI_ENV, environ=ENV, redactor=Redactor(), transport=Down())
    r = a.execute(req(Role.DIRECTOR), tmp_path)
    assert r.status == "failed" and r.retryable and "connection reset" in r.error.detail


# ------------------------------------------------------------------ fail-closed parsing


@pytest.mark.parametrize("fx", [
    openai_fx(raw="this is not json"),
    openai_fx(usage=None),
    openai_fx(content=None),
    openai_fx(content=123),
    openai_fx(usage=(-1, 2)),
])
def test_openai_malformed_responses_fail_closed(tmp_path, fx):
    a, _ = _director([fx])
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "malformed" and not (tmp_path / "out").exists()


@pytest.mark.parametrize("fx", [
    anthropic_fx(raw="<html>gateway</html>"),
    anthropic_fx(usage=None),
    anthropic_fx(blocks=[]),
    anthropic_fx(blocks=[{"type": "tool_use", "id": "x", "name": "run", "input": {}}]),
    anthropic_fx(blocks=[{"type": "text"}]),
])
def test_anthropic_malformed_responses_fail_closed(tmp_path, fx):
    a, _ = _planner([fx])
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.PLANNER), tmp_path)
    assert e.value.category == "malformed" and not (tmp_path / "out").exists()


def test_truncated_output_is_never_accepted(tmp_path):
    a, _ = _director([openai_fx(finish="length")])
    with pytest.raises(AdapterProtocolError, match="truncated"):
        a.execute(req(Role.DIRECTOR), tmp_path)
    b, _ = _planner([anthropic_fx(stop="max_tokens")])
    with pytest.raises(AdapterProtocolError, match="truncated"):
        b.execute(req(Role.PLANNER), tmp_path)


def test_refusal_is_reported_as_refused_with_no_artifact(tmp_path):
    a, _ = _director([openai_fx(content=None, refusal="I can't help with that")])
    r = a.execute(req(Role.DIRECTOR), tmp_path)
    assert r.status == "refused" and not r.produced_artifacts


def test_oversized_http_body_is_rejected_before_parsing(tmp_path):
    class Big:
        def send(self, request):
            return HttpResponse(200, {}, b'{"x":"' + b"a" * (MAX_HTTP_BODY_BYTES + 1) + b'"}')

    a = OpenAIDirectorAdapter(model="gpt-test", credential_env=OPENAI_ENV, environ=ENV, redactor=Redactor(), transport=Big())
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "oversized"


def test_oversized_model_output_is_rejected_not_truncated(tmp_path):
    small = [ExpectedOutput(logical_name="out.md", media_type="text/markdown", artifact_class="plan", max_bytes=10)]
    a, _ = _director([openai_fx(content="x" * 11)])
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.DIRECTOR, outputs=small), tmp_path)
    assert e.value.category == "oversized" and not (tmp_path / "out" / "out.md").exists()
    ok = FixtureTransport([openai_fx(content="x" * 10)])
    b = OpenAIDirectorAdapter(model="gpt-test", credential_env=OPENAI_ENV, environ=ENV, redactor=Redactor(), transport=ok)
    assert b.execute(req(Role.DIRECTOR, outputs=small), tmp_path).status == "succeeded"       # the limit itself is allowed


def test_adapters_need_exactly_one_expected_output(tmp_path):
    two = [ExpectedOutput(logical_name="a.md", media_type="text/markdown", artifact_class="plan"),
           ExpectedOutput(logical_name="b.md", media_type="text/markdown", artifact_class="plan")]
    a, _ = _director([openai_fx()])
    with pytest.raises(AdapterProtocolError, match="exactly one"):
        a.execute(req(Role.DIRECTOR, outputs=two), tmp_path)


# ------------------------------------------------------------------ credentials + host allowlist


@pytest.mark.parametrize("env", [{}, {OPENAI_ENV: ""}])
def test_missing_credentials_fail_closed_before_any_request(tmp_path, env):
    a, t = _director([openai_fx()], env=env)
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "no_credentials" and t.calls == []


def test_the_credential_is_read_from_the_environment_at_call_time_and_registered_for_redaction(tmp_path):
    red = Redactor()
    env = dict(ENV)
    a, _ = _director([openai_fx(content=f"leak {KEY_OPENAI} end")], env=env, red=red)
    r = a.execute(req(Role.DIRECTOR), tmp_path)
    text = (tmp_path / r.produced_artifacts[0].path).read_text()
    assert KEY_OPENAI not in text and "[REDACTED]" in text
    # never stored on the adapter (the TRANSPORT's fixture legitimately contains the planted key as response text)
    assert not hasattr(a, "api_key") and all(KEY_OPENAI not in repr(v) for k, v in vars(a).items() if k != "transport")


def test_unlisted_host_is_refused_even_if_the_url_is_tampered(tmp_path):
    a, t = _director([openai_fx()])
    a.URL = "https://evil.example/v1/chat/completions"                  # simulate a config/redirect attack on the instance
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "policy" and t.calls == []
    b, t2 = _planner([anthropic_fx()])
    b.URL = "http://api.anthropic.com/v1/messages"                      # plain http is refused too
    with pytest.raises(AdapterProtocolError):
        b.execute(req(Role.PLANNER), tmp_path)
    assert t2.calls == []


def test_fixture_transport_is_strict_unknown_requests_do_not_get_a_default_answer(tmp_path):
    a, _ = _director([anthropic_fx()])                                  # only an Anthropic fixture is loaded
    with pytest.raises(FixtureMismatch):
        a.execute(req(Role.DIRECTOR), tmp_path)
    assert HttpRequest("GET", "u", {}, b"") is not None
