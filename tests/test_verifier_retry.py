"""A verifier failure is retried ONCE per task; a second verifier failure is final. Hash mismatches are never retried."""
from __future__ import annotations

import hashlib

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.limits import Limits
from airos.domain.schemas import (
    AcceptanceResult,
    AgentResponse,
    ExpectedOutput,
    ProducedArtifact,
    ResourceLimits,
    ResourceUsage,
    Role,
)
from airos.domain.states import RunState, TaskState
from tests.conftest import HUMAN, approve_fully, make_contract

OUT = [ExpectedOutput(logical_name="o.txt", media_type="text/plain", artifact_class="work_product")]


def _setup(repo, tmp_path, script, max_retries=2, adapter=None):
    adapters = all_mock_adapters()
    adapters[Role.EXECUTOR] = adapter or MockAdapter(Role.EXECUTOR, script=script)
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], adapters, {})
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w")
    t = approve_fully(repo, repo.create_task(make_contract(expected_output=OUT, resource_limits=ResourceLimits(
        max_usd_micro=1_000_000, max_retries=max_retries)), HUMAN).task)
    return w, t


def test_first_verifier_failure_is_retried_second_is_not(repo, clock, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r: "invalid_output")
    w.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.QUEUED and cur.error_history[-1].error_class == "schema_violation"   # retried once
    clock.advance(Limits().backoff_ms(1))
    w.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.FAILED and cur.attempt_count == 2                                    # NOT retried again
    assert sum(e.error_class == "schema_violation" for e in cur.error_history) == 2
    clock.advance(10 * Limits().backoff_ms(2))
    w.tick()
    assert repo.get_task(t.task_id).attempt_count == 2 and len(repo.list_runs(t.task_id)) == 2           # no third run, ever
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve"} == {e.reserve_ref for e in ledger if e.kind.value == "release"}


def test_first_verifier_failure_then_success_completes(repo, clock, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r: "invalid_output" if r.attempt == 1 else "success")
    w.tick()
    clock.advance(Limits().backoff_ms(1))
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW


def test_only_verifier_failures_consume_the_verifier_retry(repo, clock, tmp_path):
    seq = {1: "transient_failure", 2: "invalid_output", 3: "invalid_output"}
    w, t = _setup(repo, tmp_path, lambda r: seq[r.attempt], max_retries=5)
    for _ in range(3):
        w.tick()
        clock.advance(Limits().backoff_ms(3))
    cur = repo.get_task(t.task_id)
    assert cur.attempt_count == 3 and cur.state is TaskState.FAILED                  # transient, verifier(retry), verifier(final)
    assert [e.error_class for e in cur.error_history] == ["transient", "schema_violation", "schema_violation"]


def test_no_verifier_retry_when_the_task_has_no_retries_at_all(repo, clock, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r: "invalid_output", max_retries=0)
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.FAILED and repo.get_task(t.task_id).attempt_count == 1


class LyingHash(MockAdapter):
    """Writes a file but claims a different sha256 for it."""

    def execute(self, request, workdir):
        out = workdir / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "o.txt").write_bytes(b"real bytes")
        return AgentResponse(
            task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded",
            resource_usage=ResourceUsage(tokens_in=0, tokens_out=0, usd_micro=0, wall_ms=1, measured_by="adapter", model="mock"),
            produced_artifacts=[ProducedArtifact(logical_name="o.txt", path="out/o.txt", sha256=hashlib.sha256(b"other").hexdigest(),
                                                 size_bytes=10, media_type="text/plain", artifact_class="work_product")],
            acceptance_results=[AcceptanceResult(criterion_id=c.id, self_reported="pass") for c in request.acceptance_criteria])


def test_hash_mismatch_is_never_retried(repo, tmp_path):
    w, t = _setup(repo, tmp_path, None, adapter=LyingHash(Role.EXECUTOR))
    w.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.FAILED and cur.attempt_count == 1 and cur.error_history[-1].error_class == "hash_mismatch"
    assert repo.list_runs(t.task_id)[0].state is RunState.FAILED
