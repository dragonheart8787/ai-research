"""REAL container tests. WRITTEN IN M4a, NEVER EXECUTED THERE: no Docker daemon existed in the build environment, so every test in this module is
SKIPPED and must be reported as SKIPPED, never as passing. They run only when BOTH hold:
  1. AIROS_TEST_DOCKER=1 is set (explicit opt-in by the human on the VM), and
  2. a Docker daemon socket exists (DOCKER_HOST=unix://... or /var/run/docker.sock or /run/user/<uid>/docker.sock),
and they need a local test image (AIROS_TEST_IMAGE, default airos-local/test:dev: a small busybox-like image with id, touch, grep, env, ls, wget).
See docs/architecture/M4B-HANDOFF.md for the exact commands and what each result proves."""
from __future__ import annotations

import os
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from airos.agents.real.cli import CliInvocation
from airos.auth import build_adapter_env
from airos.sandbox.runner import ContainerCliRunner, SubprocessDockerClient
from airos.sandbox.spec import SandboxPolicy


def _socket_exists() -> bool:
    candidates = []
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        candidates.append(host[len("unix://"):])
    candidates += ["/var/run/docker.sock", f"/run/user/{os.getuid()}/docker.sock"]
    for c in candidates:
        try:
            if stat.S_ISSOCK(os.stat(c).st_mode):
                return True
        except OSError:
            continue
    return False


if os.environ.get("AIROS_TEST_DOCKER") != "1":
    _SKIP = "SKIPPED: AIROS_TEST_DOCKER=1 is not set (real-container tests are opt-in and run only on the human's VM)"
elif not _socket_exists():
    _SKIP = "SKIPPED: no Docker daemon socket found"
else:
    _SKIP = ""

pytestmark = [pytest.mark.docker_real, pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "docker available")]

IMAGE = os.environ.get("AIROS_TEST_IMAGE", "airos-local/test:dev")
ROLE = "planner"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=120)  # noqa: S603,S607


@pytest.fixture(scope="module", autouse=True)
def _image_present():
    if _docker("image", "inspect", IMAGE, check=False).returncode != 0:
        pytest.skip(f"SKIPPED: test image {IMAGE!r} is not present locally (build or load it first)")


@pytest.fixture(scope="module")
def internal_network():
    name = f"airos-net-{ROLE}"
    _docker("network", "create", "--internal", name)
    yield name
    _docker("network", "rm", name, check=False)


@pytest.fixture
def runner(tmp_path: Path, internal_network):
    pol = SandboxPolicy(run_root=tmp_path / "ws", images={ROLE: IMAGE, "executor": IMAGE, "director": IMAGE}, require_image_digest=False,
                        docker_host=os.environ.get("DOCKER_HOST"))
    return ContainerCliRunner(pol, SubprocessDockerClient(), {ROLE: "AIROS_CLAUDE_TOKEN_PLANNER"})


def _run(runner: ContainerCliRunner, *argv: str, token: str | None = None):
    env = build_adapter_env(ROLE, credential_value=token)
    return runner.run(CliInvocation(argv=argv, env=env, role=ROLE, run_id=uuid.uuid4().hex[:16], timeout_s=60))


def test_runs_as_the_unprivileged_uid(runner):
    r = _run(runner, "id", "-u")
    assert (r.returncode, r.stdout.strip()) == (0, "10001")


def test_root_filesystem_is_read_only_but_work_and_outputs_are_writable_and_inputs_are_not(runner):
    assert _run(runner, "touch", "/x").returncode != 0
    assert _run(runner, "touch", "/work/a").returncode == 0
    assert _run(runner, "touch", "/outputs/a").returncode == 0
    assert _run(runner, "touch", "/inputs/a").returncode != 0


def test_all_capabilities_are_dropped_and_no_new_privileges_is_set(runner):
    assert "0000000000000000" in _run(runner, "grep", "CapEff", "/proc/self/status").stdout
    assert _run(runner, "grep", "NoNewPrivs", "/proc/self/status").stdout.split()[-1] == "1"


def test_the_docker_socket_and_host_paths_are_not_visible(runner):
    assert _run(runner, "ls", "/var/run/docker.sock").returncode != 0
    assert _run(runner, "ls", "/home").stdout.strip() in ("", "ls: /home: No such file or directory")


def test_the_host_environment_does_not_leak_and_only_the_named_token_is_forwarded(runner, monkeypatch):
    monkeypatch.setenv("AIROS_HOST_ONLY_MARKER", "should-not-appear")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-appear")
    token = "sk-ant-" + "oat01-realcontainer-" + "Z" * 20
    out = _run(runner, "env", token=token).stdout
    assert "should-not-appear" not in out and "ANTHROPIC_API_KEY" not in out and "AIROS_HOST_ONLY_MARKER" not in out
    assert f"CLAUDE_CODE_OAUTH_TOKEN={token}" in out                                # forwarded into THIS container only
    argv_seen = _docker("ps", "-a", "--no-trunc", "--format", "{{.Command}}").stdout
    assert token not in argv_seen


def test_the_internal_network_has_no_route_out(runner):
    r = _run(runner, "wget", "-T", "3", "-q", "-O", "-", "http://1.1.1.1")
    assert r.returncode != 0
