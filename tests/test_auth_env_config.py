"""A2/A3/A5: no API-key mode anywhere; adapter environments are built from an allowlist (never inherited); config is strict,
fail-closed and secret-free; every Claude-driven adapter refuses to start without the human's usage-credits-OFF attestation."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from airos.agents.adapters import all_mock_adapters
from airos.agents.real.cli import FixtureCliRunner
from airos.agents.real.factory import real_factories
from airos.agents.registry import AgentRegistry, AgentSpec, RegistryPolicy
from airos.auth import (
    BASE_ENV,
    AuthMode,
    EnvPolicyViolation,
    build_adapter_env,
    is_forbidden_env_name,
    scan_host_environment,
    validate_credential_host_var,
    validate_requested_env,
)
from airos.config import ConfigError, load_agents_config
from airos.domain.schemas import Role
from airos.domain.states import BlockReason, TaskState
from airos.errors import AdapterUnavailable
from tests.conftest import HUMAN, TEST_PRICES, approve_fully
from tests.real_helpers import (
    ENV,
    FORBIDDEN_HOST_ENV,
    PLANNER_ENV,
    POLICY,
    TOKEN_PLANNER,
    ScriptedLoop,
    claude_fx,
    codex_exec_fx,
    codex_status_fx,
    real_world,
    role_contract,
)

CONFIG = Path(__file__).resolve().parent.parent / "config" / "agents.toml"
NOW = date(2026, 11, 7)


# ------------------------------------------------------------------ A2: there is no api_key mode


def test_api_key_is_not_an_auth_mode_anywhere():
    assert {m.value for m in AuthMode} == {"subscription_oauth", "manual_handoff"}
    with pytest.raises(ValueError):
        AuthMode("api_key")
    reg = AgentRegistry([AgentSpec(role="planner", kind="real", credential_env=PLANNER_ENV, model="claude-opus-5-5", auth_mode="api_key")],
                        all_mock_adapters(), ENV, TEST_PRICES, real_factories(cli_runner=FixtureCliRunner([])), policy=POLICY)
    with pytest.raises(AdapterUnavailable, match="does not exist"):
        reg.resolve("planner")


def test_a_role_cannot_use_another_roles_auth_mode():
    reg = AgentRegistry([AgentSpec(role="planner", kind="real", credential_env=PLANNER_ENV, model="claude-opus-5-5", auth_mode="manual_handoff")],
                        all_mock_adapters(), ENV, TEST_PRICES, real_factories(cli_runner=FixtureCliRunner([])), policy=POLICY)
    with pytest.raises(AdapterUnavailable, match="not valid for"):
        reg.resolve("planner")


def test_source_tree_has_no_raw_http_provider_client_and_reads_no_api_key_variable():
    src = Path(__file__).resolve().parent.parent / "src" / "airos"
    text = "\n".join(p.read_text() for p in src.rglob("*.py") if p.name not in ("auth.py", "config.py"))
    for needle in ("api.openai.com", "api.anthropic.com", "Authorization:", "x-api-key", "UrllibTransport"):
        assert needle not in text, needle
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"):
        assert f'environ["{name}"]' not in text and f"getenv(\"{name}\")" not in text


# ------------------------------------------------------------------ A3: adapter environments


@pytest.mark.parametrize("name", sorted(FORBIDDEN_HOST_ENV) + ["anthropic_api_key", "MY_SERVICE_API_KEY", "FOO_AUTH_TOKEN", "AWS_REGION",
                                                              "OPENAI_ORG_ID", "AZURE_OPENAI_ENDPOINT", "GEMINI_MODEL"])
def test_forbidden_names_are_recognised_and_refused(name):
    assert is_forbidden_env_name(name)
    with pytest.raises(EnvPolicyViolation):
        validate_requested_env("executor", [name])
    with pytest.raises(EnvPolicyViolation):
        validate_credential_host_var(name)


def test_nothing_is_pass_through_allowlisted_by_default():
    for role in ("planner", "executor", "director", "chatgpt_work", "ai_scientist"):
        with pytest.raises(EnvPolicyViolation, match="allowlist"):
            validate_requested_env(role, ["HARMLESS_VAR"])
        assert validate_requested_env(role, []) == []


def test_the_built_environment_is_constants_plus_at_most_one_credential():
    planner = build_adapter_env("planner", credential_value=TOKEN_PLANNER)
    assert set(planner) == {*BASE_ENV, "HOME", "CLAUDE_CODE_OAUTH_TOKEN"} and planner["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_PLANNER
    assert set(build_adapter_env("planner")) == {*BASE_ENV, "HOME"}                                   # no credential, none invented
    director = build_adapter_env("director", credential_value=TOKEN_PLANNER)                           # a token is never given to the Director
    assert set(director) == {*BASE_ENV, "HOME", "CODEX_HOME"} and TOKEN_PLANNER not in director.values()
    for role in ("chatgpt_work", "ai_scientist"):
        assert set(build_adapter_env(role, credential_value=TOKEN_PLANNER)) == {*BASE_ENV, "HOME"}
    assert not any(is_forbidden_env_name(k) for r in ("planner", "executor", "director") for k in build_adapter_env(r))


def test_scan_reports_names_only():
    found = scan_host_environment({**FORBIDDEN_HOST_ENV, "PATH": "/bin", "HOME": "/root"})
    assert found == sorted(FORBIDDEN_HOST_ENV) and not any(v in " ".join(found) for v in FORBIDDEN_HOST_ENV.values())


def test_a_host_environment_full_of_forbidden_variables_reaches_no_adapter(real_repo, tmp_path):
    repo = real_repo
    host = {**FORBIDDEN_HOST_ENV, "PATH": "/host/bin", "HOME": "/home/operator", "SECRET_THING": "operator-private"}
    plan_fx = claude_fx(usage=(1, 1))
    w = real_world(repo, tmp_path, fixtures=[codex_status_fx(), codex_exec_fx(usage=(1, 1)), plan_fx], roles=("director", "planner"),
                   host_environ=host)
    for role in ("director", "planner"):
        approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)
    w.worker.start()                                                                                     # startup scan: names only
    w.worker.tick()
    assert len(w.runner.calls) == 3
    for call in w.runner.calls:
        assert not any(k in call.env for k in (*FORBIDDEN_HOST_ENV, "SECRET_THING")), sorted(call.env)  # nothing inherited from the host
        assert all(v not in call.env.values() for v in host.values())
        assert not any(is_forbidden_env_name(k) for k in call.env)
        assert call.env["PATH"] != "/host/bin" and call.env["HOME"] != "/home/operator"
    audited = [e for e in repo.iter_events() if e.event_type == "env.forbidden_present_on_host"]
    assert audited and set(audited[0].payload["names"]) == set(FORBIDDEN_HOST_ENV)
    assert all(not any(v in e.model_dump_json() for v in FORBIDDEN_HOST_ENV.values() if len(v) > 4) for e in audited)   # names, never values


def test_config_trying_to_pass_a_forbidden_variable_is_refused_and_audited(repo, tmp_path):
    spec = AgentSpec(role="executor", kind="real", credential_env="TOK", model="claude-sonnet-5-5", options={"pass_env": "OPENAI_API_KEY"})
    reg = AgentRegistry([spec], all_mock_adapters(), {"TOK": "x" * 20}, TEST_PRICES, real_factories(executor_loop=ScriptedLoop()), policy=POLICY)
    t = approve_fully(repo, repo.create_task(role_contract("executor"), HUMAN).task)
    with pytest.raises(AdapterUnavailable, match="refused"):
        reg.route(repo, t)
    kinds = [e.event_type for e in repo.iter_events()]
    assert "env.forbidden_refused" in kinds and "agent.route_failed" in kinds
    # a credential sourced FROM a forbidden variable is refused as well
    bad = AgentRegistry([AgentSpec(role="executor", kind="real", credential_env="ANTHROPIC_API_KEY", model="claude-sonnet-5-5")],
                        all_mock_adapters(), {"ANTHROPIC_API_KEY": "x" * 20}, TEST_PRICES, real_factories(executor_loop=ScriptedLoop()), policy=POLICY)
    with pytest.raises(AdapterUnavailable, match="refused"):
        bad.resolve("executor")


# ------------------------------------------------------------------ A5: shipped config, strictness, attestation, model allowlist


def test_the_shipped_config_is_all_disabled_unattested_and_secret_free():
    cfg = load_agents_config(CONFIG, today=NOW)
    assert cfg.policy.usage_credits_attested_off is False and cfg.policy.attested_on is None
    assert {s.role: s.enabled for s in cfg.specs} == {r: False for r in ("director", "planner", "executor", "chatgpt_work", "ai_scientist")}
    assert cfg.quota_probe_enabled is False
    assert cfg.limits.agent_sdk_credit_monthly_cap_usd_micro == 15_000_000
    assert cfg.limits.role_allocations_usd_micro == {"executor": 10_000_000, "planner": 3_000_000, "ai_scientist": 0}
    assert cfg.limits.director_max_runs_per_day == 0
    assert {s.role: s.model for s in cfg.specs}["planner"] == "claude-opus-5-5" and {s.role: s.model for s in cfg.specs}["executor"] == "claude-sonnet-5-5"
    assert {s.role: s.model for s in cfg.specs}["director"] is None                                       # set by the human
    assert not any(s.auth_mode == "api_key" for s in cfg.specs)
    text = CONFIG.read_text()
    assert "sk-" not in text and "api_key =" not in text.lower()


def _cfg(extra: str = "", *, attested: str = "false", on: str = "") -> str:
    return CONFIG.read_text().replace("usage_credits_attested_off = false", f"usage_credits_attested_off = {attested}").replace(
        'attested_on = ""', f'attested_on = "{on}"') + extra


@pytest.mark.parametrize("text,why", [
    ('[roles.planner]\nauth_mode = "api_key"\n', "unknown key|does not exist|duplicate|invalid TOML"),
    ("[unknown]\nx = 1\n", "unknown key"),
    ('x = "sk-ant-' + "oat01-" + "A" * 30 + '"', "unknown key|secret-looking"),
])
def test_config_rejects_unknown_sections_api_key_modes_and_secret_looking_values(text, why):
    with pytest.raises(ConfigError, match=why):
        load_agents_config(CONFIG.read_text() + "\n" + text, today=NOW)


def test_config_refuses_api_key_settings_and_bad_modes_and_forbidden_env():
    base = CONFIG.read_text()
    with pytest.raises(ConfigError, match="API-key"):
        load_agents_config(base.replace('credential_env = "AIROS_CLAUDE_TOKEN_PLANNER"', 'api_key_env = "X"'), today=NOW)
    with pytest.raises(ConfigError, match="API-key|does not exist"):
        load_agents_config(base.replace('auth_mode = "subscription_oauth"     # Codex', 'auth_mode = "api_key"     # Codex'), today=NOW)
    with pytest.raises(ConfigError, match="API-key|refused|allowlist"):
        load_agents_config(base.replace('model = "claude-opus-5-5"\ncredential_env', 'model = "claude-opus-5-5"\npass_env = "OPENAI_API_KEY"\ncredential_env'), today=NOW)
    with pytest.raises(ConfigError, match="API-key|refused"):
        load_agents_config(base.replace('"AIROS_CLAUDE_TOKEN_PLANNER"', '"ANTHROPIC_API_KEY"'), today=NOW)
    with pytest.raises(ConfigError, match="refused"):                                  # a forbidden name the text scan does not catch
        load_agents_config(base.replace('"AIROS_CLAUDE_TOKEN_PLANNER"', '"AWS_ACCESS_KEY_ID"'), today=NOW)


@pytest.mark.parametrize("model", ["claude-fable-5-1", "Claude-FABLE-5", "claude-opus-5-4", "claude-sonnet-5-5", "gpt-5", ""])
def test_planner_model_must_be_exactly_the_allowlisted_one(model):
    text = CONFIG.read_text().replace('model = "claude-opus-5-5"', f'model = "{model}"')
    if model == "":
        assert {s.role: s.model for s in load_agents_config(text, today=NOW).specs}["planner"] is None
        return
    with pytest.raises(ConfigError, match="allowlist|Fable|fable"):
        load_agents_config(text, today=NOW)


def test_the_executor_may_not_use_the_planner_model_and_the_allowlists_cannot_be_widened():
    with pytest.raises(ConfigError):
        load_agents_config(CONFIG.read_text().replace('model = "claude-sonnet-5-5"', 'model = "claude-opus-5-5"'), today=NOW)
    with pytest.raises(ConfigError, match="must be exactly"):
        load_agents_config(CONFIG.read_text().replace('executor = ["claude-sonnet-5-5"]', 'executor = ["claude-sonnet-5-5", "claude-fable-5-1"]'), today=NOW)


def test_attestation_parsing_and_dates():
    on = load_agents_config(_cfg(attested="true", on="2026-11-01"), today=NOW)
    assert on.policy.attestation_problem() is None
    assert load_agents_config(_cfg(), today=NOW).policy.attestation_problem() is not None
    assert "attested_on is missing" in (load_agents_config(_cfg(attested="true"), today=NOW).policy.attestation_problem() or "")
    assert "future" in (load_agents_config(_cfg(attested="true", on="2027-01-01"), today=NOW).policy.attestation_problem() or "")
    with pytest.raises(ConfigError, match="ISO date"):
        load_agents_config(_cfg(attested="true", on="yesterday"), today=NOW)
    with pytest.raises(ConfigError, match="boolean"):
        load_agents_config(_cfg(attested='"yes"'), today=NOW)


@pytest.mark.parametrize("role,model,cred", [("planner", "claude-opus-5-5", PLANNER_ENV), ("executor", "claude-sonnet-5-5", "AIROS_CLAUDE_TOKEN_EXECUTOR")])
def test_every_claude_driven_adapter_refuses_to_start_until_the_human_attests(role, model, cred):
    spec = AgentSpec(role=role, kind="real", credential_env=cred, model=model)
    facts = real_factories(cli_runner=FixtureCliRunner([]), executor_loop=ScriptedLoop())
    for policy, ok in ((RegistryPolicy(), False),
                       (RegistryPolicy(usage_credits_attested_off=True), False),            # no date
                       (RegistryPolicy(usage_credits_attested_off=True, attested_on=date(2027, 1, 1), today=lambda: NOW), False),
                       (RegistryPolicy(usage_credits_attested_off=True, attested_on=date(2026, 11, 1), today=lambda: NOW), True)):
        reg = AgentRegistry([spec], all_mock_adapters(), ENV, TEST_PRICES, facts, policy=policy)
        if ok:
            assert not reg.resolve(role).is_mock
        else:
            with pytest.raises(AdapterUnavailable, match="refuses to start"):
                reg.resolve(role)


def test_a_planner_task_is_blocked_unavailable_without_the_attestation(real_repo, tmp_path):
    repo = real_repo
    w = real_world(repo, tmp_path, roles=("planner",), policy=RegistryPolicy())
    t = approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.ADAPTER_UNAVAILABLE and w.runner.calls == []
    assert any("usage_credits_attested_off" in str(e.payload.get("reason")) for e in repo.iter_events() if e.event_type == "agent.route_failed")


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-4", "claude-sonnet-5-5", "gpt-5", None, ""])
def test_registry_refuses_models_off_the_planner_allowlist_including_every_fable(model):
    spec = AgentSpec(role="planner", kind="real", credential_env=PLANNER_ENV, model=model)
    reg = AgentRegistry([spec], all_mock_adapters(), ENV, TEST_PRICES, real_factories(cli_runner=FixtureCliRunner([])), policy=POLICY)
    with pytest.raises(AdapterUnavailable, match="allowlist|Fable|no model"):
        reg.resolve("planner")


def test_director_and_planner_default_disabled_and_the_director_needs_its_own_attestation(repo):
    cfg = load_agents_config(CONFIG, today=NOW)
    assert not {s.role: s for s in cfg.specs}["director"].enabled and not {s.role: s for s in cfg.specs}["planner"].enabled
    spec = AgentSpec(role="director", kind="real", model="director-test-model", auth_mode="subscription_oauth")        # attestation missing
    reg = AgentRegistry([spec], all_mock_adapters(), {}, TEST_PRICES, real_factories(cli_runner=FixtureCliRunner([])), policy=POLICY)
    with pytest.raises(AdapterUnavailable, match="no API key was created"):
        reg.resolve("director")
    assert Role.DIRECTOR
