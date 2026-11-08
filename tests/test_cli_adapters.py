"""Planner (`claude -p`) and Director (`codex exec`) adapters behind the CliRunner seam. SYNTHETIC fixtures only (tests/fixtures/README.md):
these prove OUR invocation shape, environment, and fail-closed parsing, not that the real CLIs behave this way."""
from __future__ import annotations

import json

import pytest

from airos.agents.real.cli import FixtureCliRunner, FixtureMismatch, HostSubprocessRunner
from airos.agents.real.cli_adapters import ClaudePlannerAdapter, CodexDirectorAdapter, PlanProposal
from airos.agents.real.common import AdapterProtocolError
from airos.domain.schemas import ArtifactClass, ExpectedOutput, Role
from airos.redaction import Redactor
from tests.real_helpers import (
    ENV,
    PLANNER_ENV,
    TOKEN_PLANNER,
    claude_fx,
    codex_exec_fx,
    codex_status_fx,
    plan_json,
    req,
)

PLAN_OUT = [ExpectedOutput(logical_name="plan.json", media_type="application/json", artifact_class="plan")]


def _planner(fixtures, env=None, red=None, runner=None):
    r = runner or FixtureCliRunner(fixtures)
    return ClaudePlannerAdapter(model="claude-opus-5-5", credential_env=PLANNER_ENV, environ=ENV if env is None else env,
                                redactor=red or Redactor(), runner=r), r


def _director(fixtures, attested=True, red=None, runner=None):
    r = runner or FixtureCliRunner(fixtures)
    return CodexDirectorAdapter(model="director-test-model", environ={}, redactor=red or Redactor(), runner=r, no_api_key_attested=attested), r


# ------------------------------------------------------------------ planner: invocation contract


def test_planner_invocation_has_the_expected_shape_and_an_explicit_minimal_environment(tmp_path):
    a, r = _planner([claude_fx()])
    a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    [inv] = r.calls
    assert inv.argv[:2] == ("claude", "-p") and "--output-format" in inv.argv and inv.argv[inv.argv.index("--output-format") + 1] == "json"
    assert inv.argv[inv.argv.index("--model") + 1] == "claude-opus-5-5"
    assert inv.argv[-2:] == ("--model", "claude-opus-5-5") or "--tools" in inv.argv          # tools are disabled
    assert inv.argv[inv.argv.index("--tools") + 1] == ""
    # the complete environment: constants + exactly one credential, named CLAUDE_CODE_OAUTH_TOKEN
    assert set(inv.env) == {"PATH", "LANG", "LC_ALL", "TZ", "HOME", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert inv.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_PLANNER
    # prompt goes over stdin, never argv; the token is never in argv or stdin
    assert "investigate topic X" in (inv.stdin or "") and TOKEN_PLANNER not in " ".join(inv.argv) and TOKEN_PLANNER not in (inv.stdin or "")
    assert TOKEN_PLANNER not in repr(inv)


def test_planner_success_writes_a_validated_plan_and_reports_the_subscription_auth_path(tmp_path):
    a, _ = _planner([claude_fx(plan_json(3), usage=(120, 40))])
    resp = a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert resp.status == "succeeded" and not a.is_mock and resp.auth_path == "subscription_oauth"
    assert (resp.resource_usage.tokens_in, resp.resource_usage.tokens_out, resp.resource_usage.model, resp.resource_usage.usd_micro) == (120, 40, "claude-opus-5-5", 0)
    [p] = resp.produced_artifacts
    plan = PlanProposal.model_validate_json((tmp_path / p.path).read_text())
    assert len(plan.tasks) == 3 and p.artifact_class is ArtifactClass.PLAN


@pytest.mark.parametrize("auth,expected", [("oauth", "subscription_oauth"), ("ANTHROPIC_API_KEY", "unconfirmed:ANTHROPIC_API_KEY"),
                                           ("user", "unconfirmed:user"), (None, None)])
def test_auth_path_is_reported_exactly_as_confirmed_or_unconfirmed(tmp_path, auth, expected):
    a, _ = _planner([claude_fx(auth=auth)])
    assert a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path).auth_path == expected


@pytest.mark.parametrize("bad_result", ["not json", '{"schema_version":"plan-proposal/9","tasks":[]}', '{"tasks":[]}',
                                        json.dumps({"schema_version": "plan-proposal/1", "tasks": [{"title": "x", "objective": "y", "role": "planner"}]}),
                                        json.dumps({"schema_version": "plan-proposal/1", "tasks": []}),
                                        json.dumps({"schema_version": "plan-proposal/1", "extra": 1, "tasks": [{"title": "t", "objective": "o", "role": "executor"}]})])
def test_planner_output_that_is_not_a_valid_plan_proposal_fails_closed(tmp_path, bad_result):
    a, _ = _planner([claude_fx(bad_result)])
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert e.value.category == "malformed" and not (tmp_path / "out").exists()


@pytest.mark.parametrize("fx", [claude_fx(stdout_raw="<html>"), claude_fx(usage=None), claude_fx(stdout_raw="[1,2]"),
                                claude_fx(usage=(-1, 2)), claude_fx(result=123)])
def test_planner_malformed_cli_output_fails_closed(tmp_path, fx):
    a, _ = _planner([fx])
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert e.value.category == "malformed"


def test_planner_truncation_is_never_accepted(tmp_path):
    for fx in (claude_fx(subtype="error_max_turns"), claude_fx(stop_reason="max_tokens")):
        a, _ = _planner([fx])
        with pytest.raises(AdapterProtocolError, match="truncated|stopped"):
            a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)


def test_credit_exhaustion_is_reported_distinctly_and_is_not_retryable(tmp_path):
    a, _ = _planner([claude_fx("Claude usage limit reached. Your credit will reset later.", returncode=1, is_error=True, usage=None)])
    r = a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert r.status == "failed" and r.error.error_class == "credit_exhausted" and r.retryable is False and not r.produced_artifacts


def test_other_cli_failures_are_retryable_transient(tmp_path):
    a, _ = _planner([claude_fx("something broke", returncode=1, is_error=True, usage=None)])
    r = a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert r.status == "failed" and r.error.error_class == "transient" and r.retryable is True


def test_planner_needs_its_own_token_and_never_runs_without_it(tmp_path):
    a, r = _planner([claude_fx()], env={})
    with pytest.raises(AdapterProtocolError) as e:
        a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert e.value.category == "no_credentials" and r.calls == []


def test_a_host_runner_is_refused_no_in_process_execution_of_real_adapters(tmp_path):
    a, _ = _planner([], runner=HostSubprocessRunner())
    with pytest.raises(AdapterProtocolError, match="must run inside the sandbox") as e:
        a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)
    assert e.value.category == "policy"
    d, _ = _director([], runner=HostSubprocessRunner())
    with pytest.raises(AdapterProtocolError, match="must run inside the sandbox"):
        d.execute(req(Role.DIRECTOR), tmp_path)
    no_runner = ClaudePlannerAdapter(model="m", credential_env=PLANNER_ENV, environ=ENV, redactor=Redactor(), runner=None)
    with pytest.raises(AdapterProtocolError, match="must run inside the sandbox"):
        no_runner.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)


def test_unmatched_invocations_get_no_default_answer(tmp_path):
    a, _ = _planner([codex_exec_fx()])
    with pytest.raises(FixtureMismatch):
        a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path)


# ------------------------------------------------------------------ director: login confirmation + read-only exec


def test_director_confirms_a_chatgpt_login_then_runs_read_only_with_only_codex_home(tmp_path):
    d, r = _director([codex_status_fx(), codex_exec_fx("Brief text", usage=(120, 30))])
    resp = d.execute(req(Role.DIRECTOR), tmp_path)
    assert resp.status == "succeeded" and resp.auth_path == "subscription_oauth" and not d.is_mock
    assert (resp.resource_usage.tokens_in, resp.resource_usage.tokens_out, resp.resource_usage.model) == (120, 30, "director-test-model")
    status, run = r.calls
    assert status.argv == ("codex", "login", "status")
    assert run.argv[:2] == ("codex", "exec") and "--sandbox" in run.argv and run.argv[run.argv.index("--sandbox") + 1] == "read-only"
    assert set(run.env) == {"PATH", "LANG", "LC_ALL", "TZ", "HOME", "CODEX_HOME"} and run.env["CODEX_HOME"] == "/codex-home"
    assert (tmp_path / resp.produced_artifacts[0].path).read_text() == "Brief text"


@pytest.mark.parametrize("status_text,why", [("Logged in using an API key", "API-key"), ("Not logged in", "could not be confirmed"),
                                             ("", "could not be confirmed"), ("Logged in using ChatGPT and an API key", "API-key")])
def test_director_stays_disabled_unless_the_login_is_confirmed_as_chatgpt(tmp_path, status_text, why):
    d, r = _director([codex_status_fx(status_text), codex_exec_fx()])
    with pytest.raises(AdapterProtocolError, match=why) as e:
        d.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "billing" and len(r.calls) == 1            # the exec call was never made
    assert d.login_confirmed is False


def test_director_refuses_without_the_human_attestation_that_no_api_key_exists(tmp_path):
    d, r = _director([codex_status_fx(), codex_exec_fx()], attested=False)
    with pytest.raises(AdapterProtocolError, match="not attested") as e:
        d.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "billing" and r.calls == []


def test_director_login_is_reconfirmed_on_every_run(tmp_path):
    d, r = _director([codex_status_fx(), codex_exec_fx()])
    d.execute(req(Role.DIRECTOR), tmp_path)
    d.execute(req(Role.DIRECTOR), tmp_path / "again")
    assert [c.argv[:3] for c in r.calls].count(("codex", "login", "status")) == 2


@pytest.mark.parametrize("fx", [codex_exec_fx(message=None), codex_exec_fx(usage=None), codex_exec_fx(extra_lines=["not json"]),
                                codex_exec_fx(message=5), codex_exec_fx(extra_lines=["[1]"])])
def test_director_malformed_events_fail_closed(tmp_path, fx):
    d, _ = _director([codex_status_fx(), fx])
    with pytest.raises(AdapterProtocolError) as e:
        d.execute(req(Role.DIRECTOR), tmp_path)
    assert e.value.category == "malformed"


def test_director_credit_exhaustion_and_generic_failure(tmp_path):
    d, _ = _director([codex_status_fx(), codex_exec_fx(message=None, usage=None, returncode=1, error="You have hit your usage limit")])
    r = d.execute(req(Role.DIRECTOR), tmp_path)
    assert r.status == "failed" and r.error.error_class == "credit_exhausted" and not r.retryable
    d2, _ = _director([codex_status_fx(), codex_exec_fx(message=None, usage=None, returncode=2, stderr="boom")])
    r2 = d2.execute(req(Role.DIRECTOR), tmp_path / "x")
    assert r2.error.error_class == "transient" and r2.retryable


def test_director_output_is_redacted_before_it_is_written(tmp_path):
    secret = "sk-ant-" + "oat01-" + "leak" + "Z" * 30
    d, _ = _director([codex_status_fx(), codex_exec_fx(f"token {secret}")], red=Redactor())
    resp = d.execute(req(Role.DIRECTOR), tmp_path)
    assert secret not in (tmp_path / resp.produced_artifacts[0].path).read_text()
