"""CLI-runner seam. Real adapters drive provider CLIs/SDKs (`claude -p`, `codex exec`, the Agent SDK) only through a CliRunner.

kind:
  fixture  - replays recorded/synthetic fixtures; executes nothing (used by every test).
  container- runs the CLI inside the sandbox container (airos.sandbox; verified on the VM in M4b).
  host     - runs a process on the host. Real adapters REFUSE this kind (no in-process / host execution of real adapters);
             it exists so the environment-isolation test can prove what a spawned process actually receives.
"""
from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from airos.auth import EnvPolicyViolation, is_forbidden_env_name
from airos.errors import AirosError

MAX_CLI_OUTPUT_BYTES = 1 * 1024 * 1024


class CliError(AirosError):
    """The CLI could not be run or produced unusable output."""


class FixtureMismatch(AirosError):
    """No fixture matches the invocation (strict replay): the adapter's CLI contract drifted."""


@dataclass(frozen=True)
class CliInvocation:
    argv: tuple[str, ...]
    env: Mapping[str, str]            # the COMPLETE environment; nothing is inherited
    stdin: str | None = None
    timeout_s: int = 60
    cwd: Path | None = None

    def __repr__(self) -> str:  # never print env values (they may include a token)
        return f"CliInvocation(argv={list(self.argv)!r}, env_names={sorted(self.env)!r}, stdin=<{len(self.stdin or '')} chars>)"


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str = ""


class CliRunner(Protocol):
    kind: Literal["fixture", "container", "host"]

    def run(self, inv: CliInvocation) -> CliResult: ...


@dataclass
class FixtureCliRunner:
    """Replays fixtures: {"match": {"argv_prefix": [...], "stdin_contains": "..."}, "result": {"returncode", "stdout", "stderr"}}."""

    fixtures: list[dict[str, Any]]
    kind: Literal["fixture", "container", "host"] = "fixture"
    calls: list[CliInvocation] = field(default_factory=list)

    def run(self, inv: CliInvocation) -> CliResult:
        self.calls.append(inv)
        for name in inv.env:
            if is_forbidden_env_name(name):  # a fixture runner enforces the same rule a real one does
                raise EnvPolicyViolation(name, "forbidden variable in an adapter environment")
        for fx in self.fixtures:
            m = fx["match"]
            prefix = tuple(m.get("argv_prefix", ()))
            if inv.argv[: len(prefix)] != prefix:
                continue
            needle = m.get("stdin_contains")
            if needle is not None and needle not in (inv.stdin or ""):
                continue
            r = fx["result"]
            return CliResult(int(r["returncode"]), str(r.get("stdout", "")), str(r.get("stderr", "")))
        raise FixtureMismatch(f"no fixture matches {inv!r}")


class HostSubprocessRunner:
    """Runs argv on the HOST with exactly inv.env as its environment (shell=False). Not accepted by real adapters."""

    kind: Literal["fixture", "container", "host"] = "host"

    def run(self, inv: CliInvocation) -> CliResult:
        for name in inv.env:
            if is_forbidden_env_name(name):
                raise EnvPolicyViolation(name, "forbidden variable in an adapter environment")
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False, explicit env
            list(inv.argv), input=(inv.stdin or "").encode(), env=dict(inv.env), cwd=inv.cwd, capture_output=True,
            timeout=inv.timeout_s, check=False)
        if len(proc.stdout) > MAX_CLI_OUTPUT_BYTES:
            raise CliError(f"CLI output is {len(proc.stdout)} bytes (limit {MAX_CLI_OUTPUT_BYTES})")
        return CliResult(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")[:4000])
