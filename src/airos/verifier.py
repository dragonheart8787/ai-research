"""Deterministic response verification (M2 subset of the PROPOSAL Verifier). Never trusts self-reported results:
hashes are recomputed from bytes, machine acceptance criteria are re-checked, and unsupported checks FAIL CLOSED."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from airos.artifacts import safe_resolve_in
from airos.canonical import sha256_hex
from airos.domain.schemas import AgentRequest, AgentResponse, response_problems
from airos.errors import UnsafePath


@dataclass(frozen=True)
class VerifierResult:
    passed: bool
    problems: list[str] = field(default_factory=list)


def verify_response(request: AgentRequest, response: AgentResponse, run_dir: Path,
                    forbidden: tuple[Path, ...] = ()) -> VerifierResult:
    problems = response_problems(response, request)
    if response.status != "succeeded":
        problems.append(f"response status is {response.status!r}, not 'succeeded'")
    expected = {e.logical_name for e in request.expected_output}
    produced = {p.logical_name for p in response.produced_artifacts}
    if expected - produced:
        problems.append(f"missing expected outputs: {sorted(expected - produced)}")
    if produced - expected:
        problems.append(f"unexpected outputs: {sorted(produced - expected)}")
    max_bytes = {e.logical_name: e.max_bytes for e in request.expected_output}
    if len(response.produced_artifacts) > 64:
        problems.append(f"oversized: {len(response.produced_artifacts)} artifacts claimed (limit 64)")
    for p in response.produced_artifacts:
        if p.size_bytes > max_bytes.get(p.logical_name, 10**12):
            problems.append(f"{p.logical_name}: oversized ({p.size_bytes} bytes, limit {max_bytes[p.logical_name]})")
            continue
        try:
            data = safe_resolve_in(run_dir, p.path, forbidden).read_bytes()
        except UnsafePath as exc:
            problems.append(f"{p.logical_name}: {exc}")
            continue
        if sha256_hex(data) != p.sha256 or len(data) != p.size_bytes:
            problems.append(f"{p.logical_name}: hash_mismatch (claimed {p.sha256})")
    for c in request.acceptance_criteria:
        if c.mode != "machine" or c.check is None:
            continue
        chk = c.check
        try:
            if chk.type == "file_exists":
                safe_resolve_in(run_dir, chk.target, forbidden)
            elif chk.type == "sha256_equals":
                if sha256_hex(safe_resolve_in(run_dir, chk.target, forbidden).read_bytes()) != chk.expected:
                    problems.append(f"criterion {c.id}: sha256 differs from expected")
            else:
                problems.append(f"criterion {c.id}: check type {chk.type!r} is not supported in M2 (fail closed)")
        except UnsafePath as exc:
            problems.append(f"criterion {c.id}: {exc}")
    return VerifierResult(not problems, problems)
