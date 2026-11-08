"""Adapter responses are validated against the AgentResponse schema. Malformed, oversized, and responses claiming artifacts that
do not exist all FAIL CLOSED and are AUDITED (event adapter.response_rejected). Applies to mock and real adapters alike."""
from __future__ import annotations

import hashlib

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.limits import Limits
from airos.domain.schemas import (
    AcceptanceResult,
    AgentResponse,
    EvidenceCheck,
    ExpectedOutput,
    ProducedArtifact,
    ResourceLimits,
    ResourceUsage,
    Role,
)
from airos.domain.states import RunState, TaskState
from tests.conftest import HUMAN, approve_fully, make_contract

OUT = [ExpectedOutput(logical_name="o.txt", media_type="text/plain", artifact_class="work_product", max_bytes=100)]
USAGE = ResourceUsage(tokens_in=0, tokens_out=0, usd_micro=0, wall_ms=1, measured_by="adapter", model="mock")


class Scripted(MockAdapter):
    """An adapter whose behaviour is a function (request, workdir) -> anything; `is_mock` selectable."""

    runner_kind = "fixture"   # a non-mock adapter must declare a sandboxed runner kind to pass the worker's isolation gate

    def __init__(self, fn, is_mock=True):
        super().__init__(Role.EXECUTOR)
        self._fn = fn
        self.is_mock = is_mock

    def execute(self, request, workdir, inputs=None):
        return self._fn(request, workdir)


def _ok(request, workdir, name="o.txt", data=b"hello", **over):
    (workdir / "out").mkdir(parents=True, exist_ok=True)
    (workdir / "out" / name).write_bytes(data)
    kw = dict(logical_name=name, path=f"out/{name}", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
              media_type="text/plain", artifact_class="work_product")
    kw.update(over)
    return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded", resource_usage=USAGE,
                         produced_artifacts=[ProducedArtifact(**kw)],
                         acceptance_results=[AcceptanceResult(criterion_id=c.id, self_reported="pass") for c in request.acceptance_criteria])


def _setup(repo, tmp_path, fn, is_mock=True, max_retries=2):
    scripted = Scripted(fn, is_mock)
    adapters = all_mock_adapters()
    adapters[Role.EXECUTOR] = scripted
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], adapters, {})
    if not is_mock:  # tests the WORKER's validation of a non-mock adapter without going through budget/credential gates
        original = reg.resolve
        reg.resolve = lambda role: scripted if role == "executor" else original(role)  # type: ignore[method-assign]
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w")
    t = approve_fully(repo, repo.create_task(make_contract(expected_output=OUT, resource_limits=ResourceLimits(
        max_usd_micro=1_000_000, max_retries=max_retries)), HUMAN).task)
    return w, t


def _rejected(repo):
    return [e for e in repo.iter_events() if e.event_type == "adapter.response_rejected"]


def _no_artifacts(repo):
    return repo._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("is_mock", [True, False])
@pytest.mark.parametrize("garbage", [{"status": "succeeded"}, "plain text", 42, None, {"schema_version": "agent-response/9"}, [1, 2]])
def test_malformed_output_fails_closed_is_audited_and_retried_once(repo, tmp_path, clock, garbage, is_mock):
    w, t = _setup(repo, tmp_path, lambda r, d: garbage, is_mock)
    w.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.QUEUED and cur.error_history[-1].error_class == "schema_violation"      # first: retried once
    [ev] = _rejected(repo)
    assert ev.payload["category"] == "malformed" and ev.payload["mock"] is is_mock and ev.task_id == t.task_id
    clock.advance(Limits().backoff_ms(1))
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.FAILED and len(_rejected(repo)) == 2                  # second: final
    assert _no_artifacts(repo) and repo.verify_chain().ok


def test_adapter_building_an_invalid_response_object_is_malformed_not_a_crash(repo, tmp_path):
    def bad(request, workdir):
        return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded",
                             resource_usage=USAGE, produced_artifacts=[ProducedArtifact(
                                 logical_name="o.txt", path="../../etc/passwd", sha256="a" * 64, size_bytes=1, media_type="t/p",
                                 artifact_class="work_product")])

    w, t = _setup(repo, tmp_path, bad)
    w.tick()
    assert [e.payload["category"] for e in _rejected(repo)] == ["malformed"] and _no_artifacts(repo)


def test_claimed_artifact_that_does_not_exist_is_rejected_and_audited(repo, tmp_path):
    def ghost(request, workdir):
        return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded",
                             resource_usage=USAGE, produced_artifacts=[ProducedArtifact(
                                 logical_name="o.txt", path="out/o.txt", sha256="b" * 64, size_bytes=5, media_type="text/plain",
                                 artifact_class="work_product")],
                             acceptance_results=[AcceptanceResult(criterion_id="c1", self_reported="pass")])

    w, t = _setup(repo, tmp_path, ghost, is_mock=False)
    w.tick()
    [ev] = _rejected(repo)
    assert ev.payload["category"] == "missing_artifact" and "does not exist" in ev.payload["detail"] and ev.payload["mock"] is False
    assert repo.get_task(t.task_id).state is TaskState.QUEUED and _no_artifacts(repo)


def test_response_listing_no_artifacts_when_outputs_are_expected_is_rejected(repo, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r, d: AgentResponse(
        task_id=r.task_id, run_id=r.run_id, attempt=r.attempt, status="succeeded", resource_usage=USAGE,
        acceptance_results=[AcceptanceResult(criterion_id="c1", self_reported="pass")]))
    w.tick()
    assert _rejected(repo)[0].payload["category"] == "missing_artifact"


def test_undeclared_extra_artifact_is_rejected(repo, tmp_path):
    def extra(request, workdir):
        resp = _ok(request, workdir)
        (workdir / "out" / "extra.txt").write_bytes(b"x")
        more = ProducedArtifact(logical_name="extra.txt", path="out/extra.txt", sha256=hashlib.sha256(b"x").hexdigest(), size_bytes=1,
                                media_type="text/plain", artifact_class="work_product")
        return resp.model_copy(update={"produced_artifacts": [*resp.produced_artifacts, more]})

    w, t = _setup(repo, tmp_path, extra)
    w.tick()
    assert _rejected(repo)[0].payload["category"] == "schema" and "unexpected outputs" in _rejected(repo)[0].payload["detail"]
    assert _no_artifacts(repo)


def test_oversized_artifact_is_rejected_never_retried(repo, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r, d: _ok(r, d, data=b"x" * 101))        # limit is 100 bytes
    w.tick()
    [ev] = _rejected(repo)
    assert ev.payload["category"] == "oversized"
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.FAILED and cur.attempt_count == 1 and _no_artifacts(repo)       # policy: no retry
    # the limit itself is fine
    repo2_w, t2 = _setup(repo, tmp_path / "again", lambda r, d: _ok(r, d, data=b"x" * 100))
    repo2_w.tick()
    assert repo.get_task(t2.task_id).state is TaskState.REVIEW


def test_oversized_serialized_response_is_rejected_before_anything_is_stored(repo, tmp_path):
    def huge(request, workdir):
        resp = _ok(request, workdir)
        evidence = [EvidenceCheck(check_id=f"c{i}", type="file_exists", target="t" * 400) for i in range(2500)]
        return resp.model_copy(update={"evidence": evidence})

    w, t = _setup(repo, tmp_path, huge)
    w.tick()
    [ev] = _rejected(repo)
    assert ev.payload["category"] == "oversized" and repo.get_task(t.task_id).state is TaskState.FAILED and _no_artifacts(repo)


def test_more_than_64_claimed_artifacts_is_oversized(repo, tmp_path):
    def many(request, workdir):
        resp = _ok(request, workdir)
        one = resp.produced_artifacts[0]
        return resp.model_copy(update={"produced_artifacts": [one.model_copy(update={"logical_name": f"o{i}.txt"}) for i in range(65)]})

    w, t = _setup(repo, tmp_path, many)
    w.tick()
    assert _rejected(repo)[0].payload["category"] == "oversized" and _no_artifacts(repo)


def test_hash_mismatch_is_audited_and_never_retried(repo, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r, d: _ok(r, d, sha256="c" * 64))
    w.tick()
    [ev] = _rejected(repo)
    assert ev.payload["category"] == "hash_mismatch"
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.FAILED and cur.attempt_count == 1 and cur.error_history[-1].error_class == "hash_mismatch"


def test_rejection_events_are_redacted_and_the_chain_stays_valid(open_repo, tmp_path):
    from airos.redaction import Redactor

    secret = "rejection-" + "secret-" + "value-77"
    repo = open_repo(redactor=Redactor([secret]))

    def leaky(request, workdir):
        raise_me = {"status": f"succeeded {secret}"}
        return raise_me

    w, t = _setup(repo, tmp_path, leaky)
    w.tick()
    assert all(secret not in e.model_dump_json() for e in repo.iter_events())
    assert repo.verify_chain().ok and repo.list_runs(t.task_id)[0].state is RunState.FAILED


def test_a_valid_response_still_passes_through(repo, tmp_path):
    w, t = _setup(repo, tmp_path, lambda r, d: _ok(r, d))
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW and _rejected(repo) == []
