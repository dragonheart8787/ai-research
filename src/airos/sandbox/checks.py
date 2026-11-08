"""Check runner: the ONLY way machine checks (acceptance criteria and response evidence) are evaluated.

Fail-closed rules (M4a):
  * a check is evaluated by a CheckRunner; a runner that would execute code on the host does not exist and is structurally refused,
  * `FileCheckRunner` (the default) never executes anything: it reads files under the run directory and compares hashes,
  * a check that asks for re-execution (`reproduce`) needs a CONTAINER check runner, which is M4b: until one is supplied it is UNVERIFIABLE,
  * an unknown check type is FAILED. Only PASSED counts as a pass; UNVERIFIABLE and FAILED both fail the verification.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from airos.artifacts import safe_resolve_in
from airos.canonical import sha256_hex
from airos.domain.schemas import CheckSpec, EvidenceCheck
from airos.errors import AirosError, UnsafePath

ALLOWED_RUNNER_KINDS = frozenset({"files", "container"})
FILE_CHECK_TYPES = frozenset({"file_exists", "sha256_equals"})


@dataclass(frozen=True)
class CheckItem:
    """One machine check, normalised from an acceptance criterion (CheckSpec) or a piece of response evidence (EvidenceCheck)."""

    check_id: str
    type: str
    target: str
    expected: object = None
    reproduce: tuple[str, ...] | None = None     # argv to re-execute; present => needs a sandboxed runner

    @classmethod
    def from_spec(cls, check_id: str, spec: CheckSpec) -> CheckItem:
        return cls(check_id, spec.type, spec.target, spec.expected, tuple(spec.argv) if spec.argv is not None else None)

    @classmethod
    def from_evidence(cls, ev: EvidenceCheck) -> CheckItem:
        return cls(ev.check_id, ev.type, ev.target, ev.expected, tuple(ev.reproduce) if ev.reproduce is not None else None)


class CheckOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    outcome: CheckOutcome
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is CheckOutcome.PASSED


class CheckRunnerRefused(AirosError):
    pass


class CheckRunner(Protocol):
    kind: Literal["files", "container", "host"]

    def run(self, check: CheckItem, workdir: Path) -> CheckResult: ...


class FileCheckRunner:
    """Evaluates file_exists / sha256_equals by READING files; executes nothing."""

    kind: Literal["files", "container", "host"] = "files"

    def __init__(self, forbidden: tuple[Path, ...] = ()) -> None:
        self._forbidden = forbidden

    def run(self, check: CheckItem, workdir: Path) -> CheckResult:
        cid = check.check_id
        if check.reproduce is not None:
            return CheckResult(cid, CheckOutcome.UNVERIFIABLE,
                               "re-execution needs a sandboxed (container) check runner; nothing is ever executed on the host")
        if check.type not in FILE_CHECK_TYPES:
            return CheckResult(cid, CheckOutcome.FAILED, f"unknown check type {check.type!r} (fail closed)")
        try:
            path = safe_resolve_in(workdir, check.target, self._forbidden)
            if check.type == "file_exists":
                return CheckResult(cid, CheckOutcome.PASSED)
            if sha256_hex(path.read_bytes()) == check.expected:
                return CheckResult(cid, CheckOutcome.PASSED)
            return CheckResult(cid, CheckOutcome.FAILED, "sha256 differs from expected")
        except UnsafePath as exc:
            return CheckResult(cid, CheckOutcome.FAILED, str(exc))
        except OSError as exc:
            return CheckResult(cid, CheckOutcome.FAILED, f"cannot read target: {type(exc).__name__}")


class FailClosedCheckRunner:
    """Used when no runner is configured for a kind of check: nothing can be verified, so nothing passes."""

    kind: Literal["files", "container", "host"] = "files"

    def run(self, check: CheckItem, workdir: Path) -> CheckResult:
        return CheckResult(check.check_id, CheckOutcome.UNVERIFIABLE, "no check runner is configured")


def assert_runner_allowed(runner: CheckRunner) -> None:
    """A runner that executes on the host is refused outright."""
    if runner.kind not in ALLOWED_RUNNER_KINDS:
        raise CheckRunnerRefused(f"check runner kind {runner.kind!r} is not allowed: checks never execute on the host")


def run_checks(runner: CheckRunner, checks: Sequence[CheckItem], workdir: Path) -> list[CheckResult]:
    assert_runner_allowed(runner)
    return [runner.run(c, workdir) for c in checks]


def all_passed(results: Sequence[CheckResult]) -> bool:
    return all(r.passed for r in results)
