"""Credentials come only from environment variables and are never logged, stored in artifacts, audit events, or the database.
A secret planted in an adapter's response is redacted BEFORE storage (and the audit chain stays valid)."""
from __future__ import annotations

import hashlib
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
    KEY_ANTHROPIC,
    KEY_EXEC,
    KEY_OPENAI,
    ScriptedLoop,
    anthropic_fx,
    external_workflow,
    git,
    grant_role_budget,
    make_bare_repo,
    openai_fx,
    real_world,
    role_contract,
)

PLANTED = "planted-" + "secret-" + "Xk29Qm41Lp"        # a registered secret value (not one of the API keys)
SECRETS = (KEY_OPENAI, KEY_ANTHROPIC, KEY_EXEC, PLANTED)


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
    repo = open_repo(redactor=Redactor([PLANTED]))
    bare = make_bare_repo(tmp_path / "xchg")
    leak = f"{PLANTED} / {KEY_OPENAI} / {KEY_ANTHROPIC} / {KEY_EXEC}"
    fixtures = {"openai": [openai_fx(content=f"director says {leak}", usage=(5, 5))],
                "anthropic": [anthropic_fx(text=f"planner says {leak}", usage=(5, 5))]}

    def writer(spec):
        (spec.cwd / "out").mkdir(exist_ok=True)
        (spec.cwd / "out" / "result.md").write_text(f"executor says {leak}")

    loop = ScriptedLoop(LoopResult("done", files=["out/result.md"], tokens_in=5, tokens_out=5), writer=writer)
    cfg = HandoffConfig(repo_path=bare, allowed_authors=frozenset({"chatgpt-work@example.invalid"}), work_root=tmp_path / "cp")
    w = real_world(repo, tmp_path / "w", fixtures=fixtures, handoff=cfg, loop=loop)
    tasks = {}
    for role in ("director", "planner", "executor", "chatgpt_work"):
        tasks[role] = approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)
        grant_role_budget(repo, tasks[role], role)
    w.worker.tick()                                                                    # director/planner/executor run; handoff parks
    parked = repo.get_task(tasks["chatgpt_work"].task_id)
    req_sha = hashlib.sha256(git("show", f"main:requests/{parked.task_id}/request.json", cwd=bare).encode()).hexdigest()
    external_workflow(bare, tmp_path, parked.task_id, request_sha=req_sha, files={"notes.md": f"external says {leak}".encode()})
    repo.enqueue(parked.task_id, Event.UNBLOCK, HUMAN, parked.version)
    w.worker.tick()
    assert {r: repo.get_task(t.task_id).state for r, t in tasks.items()} == {r: TaskState.REVIEW for r in tasks}
    arts = repo.list_artifacts()
    assert len(arts) == 4
    # (1) stored artifact bytes
    for a in arts:
        data = w.svc.store.path_for(a.sha256).read_bytes()
        assert not any(s.encode() in data for s in SECRETS), a.logical_name
        assert b"[REDACTED]" in data
        assert w.svc.verify(a.artifact_id).ok                                          # registry hash == hash of the REDACTED bytes
    # (2) the content-addressed store as a whole, (3) the audit log, (4) every database byte, (5) what WE sent out (the exchange repo requests/ side; the external side wrote its own leak by design)
    assert not any(s.encode() in _all_bytes(tmp_path / "w" / "cas") for s in SECRETS)
    assert not any(s in e.model_dump_json() for e in repo.iter_events() for s in SECRETS)
    assert repo.verify_chain().ok
    repo.close()
    assert not any(s.encode() in _all_bytes(*_db_files(db_path)) for s in SECRETS)
    assert not any(s in git("log", "-p", "--all", "--", "requests", cwd=bare) for s in (KEY_OPENAI, KEY_ANTHROPIC, KEY_EXEC))


def test_credentials_come_only_from_the_environment_and_are_not_in_any_object_state(open_repo, tmp_path):
    repo = open_repo()
    w = real_world(repo, tmp_path, fixtures={"openai": [openai_fx(usage=(1, 1))], "anthropic": [anthropic_fx(usage=(1, 1))]},
                   roles=("director", "planner"))
    for role in ("director", "planner"):
        t = approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)
        grant_role_budget(repo, t, role)
    w.worker.tick()
    for role, key in (("director", KEY_OPENAI), ("planner", KEY_ANTHROPIC)):
        adapter = w.registry.resolve(role)
        assert all(key not in repr(v) for k, v in vars(adapter).items() if k != "transport")
        assert not any(key == v for v in vars(adapter).values())                         # not held as an attribute at all
    for spec in w.registry._specs.values():
        assert all(key not in repr(spec) for key in ENV.values())                         # specs carry env var NAMES only
    # after use the keys are registered with the shared redactor, so even an accidental print is masked
    assert KEY_OPENAI not in repo.redactor.redact_text(f"oops {KEY_OPENAI}") and repr(repo.redactor).startswith("Redactor(<")


def test_a_transport_error_message_containing_the_key_is_redacted_in_every_record(open_repo, tmp_path):
    repo = open_repo()
    from airos.agents.real.transport import TransportError

    class Leaky:
        def send(self, request):
            raise TransportError(f"TLS failure talking with Authorization: Bearer {KEY_OPENAI}")

    w = real_world(repo, tmp_path, roles=("director",))
    w.registry._factories[Role.DIRECTOR] = type(w.registry._factories[Role.DIRECTOR])(True, True, lambda spec, env, red: __import__(
        "airos.agents.real.llm_adapters", fromlist=["OpenAIDirectorAdapter"]).OpenAIDirectorAdapter(
            model="gpt-test", credential_env=spec.credential_env, environ=env, redactor=red, transport=Leaky()))
    w.registry._built.clear()
    t = approve_fully(repo, repo.create_task(role_contract("director"), HUMAN).task)
    grant_role_budget(repo, t, "director")
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.QUEUED and cur.error_history[-1].retryable                  # transient transport failure -> retry
    blob = repr(cur.error_history) + "".join(e.model_dump_json() for e in repo.iter_events())
    assert KEY_OPENAI not in blob and "[REDACTED]" in blob


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
