"""ContainerCliRunner against a FAKE docker client. SPEC-ONLY, ISOLATION NOT VERIFIED: this proves what we ask docker to do and what we keep
out of that request; it does not prove a container was started, confined or cleaned up."""
from __future__ import annotations

import json

import pytest

from airos.agents.real.cli import CliError, CliInvocation, CliResult, HostSubprocessRunner
from airos.agents.real.cli_adapters import ClaudePlannerAdapter, CodexDirectorAdapter
from airos.agents.real.common import AdapterProtocolError
from airos.auth import build_adapter_env
from airos.domain.schemas import ExpectedOutput, Role
from airos.redaction import Redactor
from airos.sandbox.runner import ContainerCliRunner, DockerTimeout
from tests.real_helpers import ENV, PLANNER_ENV, TOKEN_PLANNER, claude_fx, codex_exec_fx, codex_status_fx, req
from tests.sandbox_helpers import CRED_SOURCE, policy

PLAN_OUT = [ExpectedOutput(logical_name="plan.json", media_type="application/json", artifact_class="plan")]


class FakeDocker:
    """Records every call; replays scripted results by position. Starts nothing."""

    def __init__(self, results=None, timeout_on=None):
        self.calls: list[tuple[list[str], dict[str, str], str | None, int]] = []
        self.results = list(results or [])
        self.timeout_on = timeout_on

    def run(self, argv, env, stdin, timeout_s):
        self.calls.append((list(argv), dict(env), stdin, timeout_s))
        if self.timeout_on is not None and len(self.calls) == self.timeout_on:
            raise DockerTimeout("timed out")
        return self.results.pop(0) if self.results else CliResult(0, "{}", "")


def _runner(tmp_path, docker=None, **kw):
    return ContainerCliRunner(policy(tmp_path / "ws", **kw), docker or FakeDocker(), CRED_SOURCE)


def _inv(role="planner", token=TOKEN_PLANNER, run_id="run0001"):
    return CliInvocation(argv=("claude", "-p"), env=build_adapter_env(role, credential_value=token), stdin="PROMPT", timeout_s=77, role=role, run_id=run_id)


def test_the_runner_declares_kind_container_which_real_adapters_accept():
    assert ContainerCliRunner.kind == "container"   # type: ignore[comparison-overlap]


def test_a_run_builds_a_validated_spec_and_hands_docker_an_argv_array(tmp_path):
    fake = FakeDocker()
    r = _runner(tmp_path, fake)
    r.run(_inv())
    [(argv, client_env, stdin, timeout)] = fake.calls
    assert argv[:2] == ["docker", "run"] and "--interactive" in argv and argv[-2:] == ["claude", "-p"] and stdin == "PROMPT" and timeout == 77
    assert r.specs[0].role == "planner" and r.specs[0].run_id == "run0001" and r.specs[0].name == "airos-planner-run0001-1"
    for sub in ("work", "inputs", "out"):
        assert (tmp_path / "ws" / "run0001" / sub).is_dir()


def test_the_token_reaches_only_the_docker_client_environment_never_the_argv(tmp_path):
    fake = FakeDocker()
    _runner(tmp_path, fake).run(_inv())
    [(argv, client_env, stdin, _)] = fake.calls
    assert TOKEN_PLANNER not in " ".join(argv) and TOKEN_PLANNER not in (stdin or "")
    assert client_env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_PLANNER and "CLAUDE_CODE_OAUTH_TOKEN" in argv       # forwarded by NAME
    assert set(client_env) == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN"}                                      # nothing else, nothing inherited
    d = FakeDocker()
    _runner(tmp_path, d).run(CliInvocation(argv=("codex", "login", "status"), env=build_adapter_env("director"), role="director", run_id="run0002"))
    assert set(d.calls[0][1]) == {"PATH", "HOME"} and "CODEX_HOME=/codex-home" in d.calls[0][0]


def test_docker_host_is_set_explicitly_from_policy_never_inherited(tmp_path):
    fake = FakeDocker()
    _runner(tmp_path, fake, docker_host="unix:///run/user/1000/docker.sock").run(_inv())
    assert fake.calls[0][1]["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"


def test_every_invocation_gets_a_distinct_container_name(tmp_path):
    fake = FakeDocker()
    r = _runner(tmp_path, fake)
    r.run(_inv())
    r.run(_inv())
    names = [c[0][c[0].index("--name") + 1] for c in fake.calls]
    assert len(set(names)) == 2


@pytest.mark.parametrize("inv", [
    CliInvocation(argv=("x",), env={}, role="chatgpt_work", run_id="r1"),
    CliInvocation(argv=("x",), env={}, role="ai_scientist", run_id="r1"),
    CliInvocation(argv=("x",), env={}, role="", run_id="r1"),
    CliInvocation(argv=("x",), env={}, role="planner", run_id=""),
    CliInvocation(argv=("x",), env={}, role="planner", run_id="../x"),
    CliInvocation(argv=("sh", "-c", "id"), env={}, role="planner", run_id="r1"),
    CliInvocation(argv=("x",), env={"OPENAI_API_KEY": "k"}, role="planner", run_id="r1"),
    CliInvocation(argv=("x",), env={"AWS_ACCESS_KEY_ID": "k"}, role="executor", run_id="r1"),
])
def test_invocations_that_cannot_be_sandboxed_are_refused_before_docker_is_called(tmp_path, inv):
    fake = FakeDocker()
    with pytest.raises(CliError):
        _runner(tmp_path, fake).run(inv)
    assert fake.calls == []


def test_a_timeout_kills_the_container_without_the_token(tmp_path):
    fake = FakeDocker(timeout_on=1)
    r = _runner(tmp_path, fake)
    with pytest.raises(DockerTimeout):
        r.run(_inv())
    kill_argv, kill_env, _, _ = fake.calls[1]
    assert kill_argv == ["docker", "kill", "airos-planner-run0001-1"] and "CLAUDE_CODE_OAUTH_TOKEN" not in kill_env


def test_the_planner_adapter_runs_through_the_container_runner_with_a_fake_docker(tmp_path):
    fx = claude_fx()
    fake = FakeDocker([CliResult(fx["result"]["returncode"], fx["result"]["stdout"], "")])
    runner = _runner(tmp_path, fake)
    a = ClaudePlannerAdapter(model="claude-opus-5-5", credential_env=PLANNER_ENV, environ=ENV, redactor=Redactor(), runner=runner)
    resp = a.execute(req(Role.PLANNER, outputs=PLAN_OUT), tmp_path / "out")
    assert resp.status == "succeeded" and resp.auth_path == "subscription_oauth"
    [(argv, env, stdin, _)] = fake.calls
    assert "airos-net-planner" in argv and TOKEN_PLANNER not in " ".join(argv) and "investigate topic X" in (stdin or "")
    assert runner.specs[0].credential_source_env == "AIROS_CLAUDE_TOKEN_PLANNER"


def test_the_director_adapter_runs_through_the_container_runner_with_a_fake_docker(tmp_path):
    results = [CliResult(0, codex_status_fx()["result"]["stdout"], ""), CliResult(0, codex_exec_fx()["result"]["stdout"], "")]
    fake = FakeDocker(results)
    d = CodexDirectorAdapter(model="director-test-model", environ={}, redactor=Redactor(), runner=_runner(tmp_path, fake), no_api_key_attested=True)
    assert d.execute(req(Role.DIRECTOR), tmp_path / "o").status == "succeeded"
    assert [c[0][c[0].index("airos-agent-director@sha256:" + "0" * 64) + 1] for c in fake.calls] == ["codex", "codex"]
    assert all("airos-net-director" in c[0] and "--mount" in c[0] for c in fake.calls)


def test_real_adapters_refuse_a_host_runner_and_no_runner_at_all(tmp_path):
    """The M4a rule: real adapters never execute in-process or on the host."""
    p = ClaudePlannerAdapter(model="m", credential_env=PLANNER_ENV, environ=ENV, redactor=Redactor(), runner=HostSubprocessRunner())
    for adapter, r in ((p, req(Role.PLANNER, outputs=PLAN_OUT)),
                       (CodexDirectorAdapter(model="m", environ={}, redactor=Redactor(), runner=HostSubprocessRunner(), no_api_key_attested=True),
                        req(Role.DIRECTOR))):
        with pytest.raises(AdapterProtocolError, match="must run inside the sandbox"):
            adapter.execute(r, tmp_path)
    assert json.dumps({}) and HostSubprocessRunner.kind == "host"          # type: ignore[comparison-overlap]


def test_no_source_module_other_than_the_two_launchers_spawns_processes():
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "airos"
    users = sorted(str(p.relative_to(src)) for p in src.rglob("*.py")
                   if any(w in p.read_text() for w in ("subprocess.run", "subprocess.Popen", "os.system", "os.popen", "os.exec")))
    assert users == ["agents/real/cli.py", "agents/real/handoff.py", "sandbox/runner.py"]
