"""ContainerCliRunner: a CliRunner (kind="container") that executes an adapter's CLI invocation INSIDE a sandbox container, via a DockerClient.

SPEC-ONLY in M4a: it is exercised in tests only through a fake DockerClient that records the argv and environment it was given. No
daemon was available, so nothing proves a real container is started, confined, or cleaned up. The real client is `SubprocessDockerClient`.
"""
from __future__ import annotations

import contextlib
import itertools
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from airos.agents.real.cli import MAX_CLI_OUTPUT_BYTES, CliError, CliInvocation, CliResult
from airos.auth import BASE_ENV, CLAUDE_TOKEN_VAR, is_forbidden_env_name
from airos.sandbox.spec import (
    CONTAINER_ROLES,
    RunSpec,
    SandboxPolicy,
    SpecError,
    build_run_spec,
    to_docker_argv,
)


class DockerTimeout(CliError):
    pass


class DockerClient(Protocol):
    def run(self, argv: Sequence[str], env: Mapping[str, str], stdin: str | None, timeout_s: int) -> CliResult: ...


class SubprocessDockerClient:
    """Runs the `docker` CLI with EXACTLY the given environment (shell=False). This is the one place a host process is spawned for the sandbox;
    it is never reached by a test in CI (no daemon, no subprocess in tests)."""

    def run(self, argv: Sequence[str], env: Mapping[str, str], stdin: str | None, timeout_s: int) -> CliResult:
        for name in env:
            if is_forbidden_env_name(name):
                raise CliError(f"forbidden variable {name} in the docker client environment")
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, shell=False, explicit env
                list(argv), input=(stdin or "").encode(), env=dict(env), capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DockerTimeout(f"docker client timed out after {timeout_s}s") from exc
        if len(proc.stdout) > MAX_CLI_OUTPUT_BYTES:
            raise CliError(f"container output is {len(proc.stdout)} bytes (limit {MAX_CLI_OUTPUT_BYTES})")
        return CliResult(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")[:4000])


@dataclass
class ContainerCliRunner:
    policy: SandboxPolicy
    docker: DockerClient
    credential_sources: Mapping[str, str] = field(default_factory=dict)       # role -> HOST variable NAME that holds its token (names only)
    kind: Literal["fixture", "container", "host"] = "container"
    specs: list[RunSpec] = field(default_factory=list)                          # every spec built, for audit/inspection
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1), repr=False)

    def run(self, inv: CliInvocation) -> CliResult:
        if inv.role not in CONTAINER_ROLES:
            raise CliError(f"role {inv.role!r} cannot run in a container")
        if not inv.run_id:
            raise CliError("a container run needs a run_id")
        for name in inv.env:
            if is_forbidden_env_name(name):
                raise CliError(f"forbidden variable {name} in an adapter environment")
        try:
            spec = build_run_spec(self.policy, inv.role, inv.run_id, inv.argv, env=inv.env,
                                  credential_source_env=self.credential_sources.get(inv.role), instance=str(next(self._seq)))
            argv = to_docker_argv(spec, self.policy)
        except SpecError as exc:
            raise CliError(str(exc)) from exc
        run_dir = self.policy.run_root / inv.run_id
        for sub in ("work", "inputs", "out"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.specs.append(spec)
        # the docker CLIENT gets a minimal environment; the token (if any) is in it so `--env NAME` can forward it, and nowhere else
        client_env = {"PATH": BASE_ENV["PATH"], "HOME": "/nonexistent"}
        if self.policy.docker_host:
            client_env["DOCKER_HOST"] = self.policy.docker_host
        if CLAUDE_TOKEN_VAR in spec.env_from_client:
            client_env[CLAUDE_TOKEN_VAR] = inv.env[CLAUDE_TOKEN_VAR]
        try:
            return self.docker.run(argv, client_env, inv.stdin, inv.timeout_s)
        except DockerTimeout:
            with contextlib.suppress(CliError):  # best effort: a timed-out run must not leave the container running
                self.docker.run(["docker", "kill", spec.name], {k: v for k, v in client_env.items() if k != CLAUDE_TOKEN_VAR}, None, 30)
            raise
