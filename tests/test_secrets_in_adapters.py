"""Credentials come only from environment variables and are never logged, stored in artifacts, audit events, or the database.
A secret planted in an adapter's response is redacted BEFORE storage (and the audit chain stays valid)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.real.claude_executor import LoopResult
from airos.agents.real.handoff import HandoffConfig
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.schemas import (
    AcceptanceResult,
    AgentResponse,
    ExpectedOutput,
    ProducedArtifact,
    ResourceUsage,
    Role,
)
from airos.domain.states import Event, TaskState
from airos.redaction import Redactor
from tests.conftest import HUMAN, approve_fully, make_contract
from tests.real_helpers import (
    ENV,
    TOKEN_EXECUTOR,
    TOKEN_PLANNER,
    ScriptedLoop,
    claude_fx,
    codex_exec_fx,
    codex_status_fx,
    external_workflow,
    git,
    make_bare_repo,
    real_world,
    role_contract,
)

PLANTED = "planted-" + "secret-" + "Xk29Qm41Lp"        # a registered secret value (not one of the tokens)
CODEX_BLOB = '{"tokens": {"access_token": "' + "eyJ" + "x" * 12 + '", "refresh_token": "rt_' + "Zz9" * 8 + '"}}'   # a fake Codex auth.json dump
SECRETS = (TOKEN_PLANNER, TOKEN_EXECUTOR, PLANTED)


def _all_bytes(*roots: Path) -> bytes:
    blob = b""
    for root in roots:
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for f in files:
            blob += f.read_bytes()
    return blob


def _db_files(db_path):
    return [Path(str(db_path) + s) for s in ("", "-wal", "-shm") if Path(str(db_path) + s).exists()]


def test_planted_secrets_in_every_real_adapter_response_never_reach_disk_cas_audit_or_db(open_repo, tmp_path, db_path):
    from airos.domain.limits import Limits

    repo = open_repo(redactor=Redactor([PLANTED]), limits=Limits(director_max_runs_per_day=10))
    bare = make_bare_repo(tmp_path / "xchg")
    leak = f"{PLANTED} / {TOKEN_PLANNER} / {TOKEN_EXECUTOR} / {CODEX_BLOB}"
    plan = json.dumps({"schema_version": "plan-proposal/1", "tasks": [{"title": "t", "objective": f"planner says {leak}", "role": "executor"}]})
    fixtures = [codex_status_fx(), codex_exec_fx(f"director says {leak}", usage=(5, 5)), claude_fx(plan, usage=(5, 5))]

    def writer(spec):
        (spec.cwd / "out").mkdir(exist_ok=True)
        (spec.cwd / "out" / "result.md").write_text(f"executor says {leak}")

    loop = ScriptedLoop(LoopResult("done", files=["out/result.md"], tokens_in=5, tokens_out=5, auth_source="oauth"), writer=writer)
    cfg = HandoffConfig(repo_path=bare, allowed_authors=frozenset({"chatgpt-work@example.invalid"}), work_root=tmp_path / "cp")
    w = real_world(repo, tmp_path / "w", fixtures=fixtures, handoff=cfg, loop=loop)
    tasks = {}
    for role in ("director", "planner", "executor", "chatgpt_work"):
        tasks[role] = approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)
    w.worker.tick()                                                                    # director/planner/executor run; handoff parks
    parked = repo.get_task(tasks["chatgpt_work"].task_id)
    req_sha = hashlib.sha256(git("show", f"main:requests/{parked.task_id}/request.json", cwd=bare).encode()).hexdigest()
    external_workflow(bare, tmp_path, parked.task_id, request_sha=req_sha, files={"notes.md": f"external says {leak}".encode()})
    repo.enqueue(parked.task_id, Event.UNBLOCK, HUMAN, parked.version)
    w.worker.tick()
    assert {r: repo.get_task(t.task_id).state for r, t in tasks.items()} == {r: TaskState.REVIEW for r in tasks}
    arts = repo.list_artifacts()
    assert len(arts) == 4
    markers = (*SECRETS, "rt_" + "Zz9" * 8, "eyJ" + "x" * 12)
    for a in arts:
        data = w.svc.store.path_for(a.sha256).read_bytes()
        assert not any(s.encode() in data for s in markers), a.logical_name
        assert b"[REDACTED]" in data
        assert w.svc.verify(a.artifact_id).ok                                          # registry hash == hash of the REDACTED bytes
    assert not any(s.encode() in _all_bytes(tmp_path / "w" / "cas") for s in markers)
    assert not any(s in e.model_dump_json() for e in repo.iter_events() for s in markers)
    assert repo.verify_chain().ok
    repo.close()
    assert not any(s.encode() in _all_bytes(*_db_files(db_path)) for s in markers)
    # what WE sent out to the exchange repo (the external side wrote its own leak by design)
    assert not any(s in git("log", "-p", "--all", "--", "requests", cwd=bare) for s in (TOKEN_PLANNER, TOKEN_EXECUTOR))


def test_credentials_come_only_from_the_environment_and_are_not_in_any_object_state(real_repo, tmp_path):
    repo = real_repo
    w = real_world(repo, tmp_path, fixtures=[codex_status_fx(), codex_exec_fx(usage=(1, 1)), claude_fx(usage=(1, 1))],
                   roles=("director", "planner"))
    for role in ("director", "planner"):
        approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)
    w.worker.tick()
    adapter = w.registry.resolve("planner")
    assert all(TOKEN_PLANNER not in repr(v) for k, v in vars(adapter).items() if k not in ("runner",))
    assert not any(v == TOKEN_PLANNER for v in vars(adapter).values())                    # not held as an attribute at all
    for spec in w.registry._specs.values():
        assert all(tok not in repr(spec) for tok in ENV.values())                         # specs carry env var NAMES only
    # after use the token is registered with the shared redactor, so even an accidental print is masked
    assert TOKEN_PLANNER not in repo.redactor.redact_text(f"oops {TOKEN_PLANNER}") and repr(repo.redactor).startswith("Redactor(<")


def test_a_cli_failure_whose_text_contains_the_token_is_redacted_in_every_record(real_repo, tmp_path):
    repo = real_repo
    leaky = claude_fx("boom", returncode=1, is_error=True, usage=None, stderr=f"auth failure with CLAUDE_CODE_OAUTH_TOKEN={TOKEN_PLANNER}")
    w = real_world(repo, tmp_path, fixtures=[leaky], roles=("planner",))
    t = approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.QUEUED and cur.error_history[-1].retryable                  # transient failure -> retry
    blob = repr(cur.error_history) + "".join(e.model_dump_json() for e in repo.iter_events())
    assert TOKEN_PLANNER not in blob


def test_login_state_patterns_are_redacted_without_registration():
    red = Redactor()
    for text in (CODEX_BLOB, "CLAUDE_CODE_OAUTH_TOKEN=abcdefgh12345678", "OPENAI_API_KEY = zzzzzzzzzz", "token sk-ant-" + "oat01-" + "Q" * 24):
        out = red.redact_text(text)
        assert out != text and "[REDACTED]" in out
    assert "Zz9Zz9Zz9" not in red.redact_text(CODEX_BLOB)


# ------------------------------------------------------------------ the generic ingest path (any adapter, including mocks)


class Writer(MockAdapter):
    def __init__(self, data, media="text/plain", name="o.txt"):
        super().__init__(Role.EXECUTOR)
        self.data, self.media, self.name = data, media, name

    def execute(self, request, workdir, inputs=None):
        (workdir / "out").mkdir(parents=True, exist_ok=True)
        (workdir / "out" / self.name).write_bytes(self.data)
        return AgentResponse(
            task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded",
            resource_usage=ResourceUsage(tokens_in=0, tokens_out=0, usd_micro=0, wall_ms=1, measured_by="adapter", model="mock"),
            produced_artifacts=[ProducedArtifact(logical_name=self.name, path=f"out/{self.name}", sha256=hashlib.sha256(self.data).hexdigest(),
                                                 size_bytes=len(self.data), media_type=self.media, artifact_class="work_product")],
            acceptance_results=[AcceptanceResult(criterion_id=c.id, self_reported="pass") for c in request.acceptance_criteria])


def _ingest_world(repo, tmp_path, adapter, name):
    adapters = all_mock_adapters()
    adapters[Role.EXECUTOR] = adapter
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], adapters, {})
    svc = ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e"))
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), svc, "w")
    out = [ExpectedOutput(logical_name=name, media_type="text/plain", artifact_class="work_product")]
    t = approve_fully(repo, repo.create_task(make_contract(expected_output=out), HUMAN).task)
    return w, svc, t


def test_ingest_redacts_a_secret_in_a_text_artifact_after_checking_integrity_on_the_original_bytes(open_repo, tmp_path):
    repo = open_repo(redactor=Redactor([PLANTED]))
    w, svc, t = _ingest_world(repo, tmp_path, Writer(f"report {PLANTED} end".encode()), "o.txt")
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW
    [a] = repo.list_artifacts()
    stored = svc.store.path_for(a.sha256).read_bytes()
    assert PLANTED.encode() not in stored and b"[REDACTED]" in stored and a.sha256 == hashlib.sha256(stored).hexdigest()
    assert svc.verify(a.artifact_id).ok


def test_ingest_refuses_to_store_a_binary_artifact_containing_a_secret(open_repo, tmp_path):
    repo = open_repo(redactor=Redactor([PLANTED]))
    w, svc, t = _ingest_world(repo, tmp_path, Writer(b"\x00\x01" + PLANTED.encode() + b"\x02", media="application/octet-stream", name="o.bin"), "o.bin")
    w.tick()
    assert repo.get_task(t.task_id).state is TaskState.FAILED and repo.list_artifacts() == []
    assert not any(PLANTED in e.model_dump_json() for e in repo.iter_events())


@pytest.mark.parametrize("pattern_secret", ["sk-" + "ant-" + "A" * 30, "ghp_" + "B" * 30, "AKIA" + "C" * 16])
def test_pattern_secrets_are_redacted_even_when_never_registered(open_repo, tmp_path, pattern_secret):
    repo = open_repo()
    w, svc, t = _ingest_world(repo, tmp_path, Writer(f"x {pattern_secret} y".encode()), "o.txt")
    w.tick()
    [a] = repo.list_artifacts()
    assert pattern_secret.encode() not in svc.store.path_for(a.sha256).read_bytes()
