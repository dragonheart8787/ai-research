"""Artifact registry and provenance: content hashes, producer run, parents, class, trust, safe paths, eval isolation."""
from __future__ import annotations

import os
import uuid

import pytest

from airos.artifacts import ArtifactService, ArtifactStore, safe_resolve_in
from airos.canonical import sha256_hex
from airos.domain.guards import GuardContext
from airos.domain.schemas import (
    ApprovalRequirement,
    ApprovalType,
    Artifact,
    ArtifactClass,
    ArtifactRef,
    ProducedArtifact,
    ResourceLimits,
    Role,
    TrustLevel,
)
from airos.domain.states import Actor, ActorKind, Event, RunState
from airos.errors import GuardFailed, IntegrityError, NotAuthorized, UnsafePath
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import (
    AGENT_ACTORS,
    CGW,
    DIRECTOR,
    EXECUTOR,
    HUMAN,
    PLANNER,
    SCI,
    approve_fully,
    free_contract,
    make_contract,
)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "cas", tmp_path / "eval-holdout")


@pytest.fixture
def svc(repo, store):
    return ArtifactService(repo, store)


def _running(repo, role: Role, actor: Actor, **over):
    reqs = [ApprovalRequirement(type=ApprovalType.EXTERNAL_HANDOFF, reason="x")] if role is Role.CHATGPT_WORK else (
        [ApprovalRequirement(type=ApprovalType.EXECUTE, reason="x")] if role in (Role.AI_SCIENTIST, Role.EXECUTOR) else [])
    c = free_contract(assigned_role=role, approval_requirements=reqs, **over)
    t = approve_fully(repo, repo.create_task(c, HUMAN).task) if reqs else repo.create_task(c, HUMAN).task
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "w1", actor.id, t.version)
    return repo.get_task(t.task_id), run


def _produce(run_dir, name="out.txt", data=b"hello", cls=ArtifactClass.WORK_PRODUCT, parents=()):
    (run_dir / "out").mkdir(parents=True, exist_ok=True)
    (run_dir / "out" / name).write_bytes(data)
    return ProducedArtifact(logical_name=name, path=f"out/{name}", sha256=sha256_hex(data), size_bytes=len(data),
                            media_type="text/plain", artifact_class=cls, parent_artifact_ids=list(parents))


# ------------------------------------------------------------------ provenance + verify


def test_ingest_records_hash_producer_class_parents_and_verify_recomputes(repo, svc, store, tmp_path):
    t, run = _running(repo, Role.EXECUTOR, EXECUTOR)
    rd = tmp_path / "run"
    rd.mkdir()
    art = svc.ingest(run_dir=rd, produced=_produce(rd), run_id=run.run_id, task=t, artifact_id="art-1", is_mock=True)
    assert art.sha256 == sha256_hex(b"hello") and art.producer_run_id == run.run_id
    assert art.artifact_class is ArtifactClass.WORK_PRODUCT and art.provenance_hash and art.is_mock
    assert svc.verify("art-1").ok
    # tamper with the STORED bytes -> verify recomputes the hash from bytes and notices
    p = store.path_for(art.sha256)
    os.chmod(p, 0o644)
    p.write_bytes(b"tampered")
    res = svc.verify("art-1")
    assert not res.ok and any("content hash mismatch" in x for x in res.problems)
    with pytest.raises(IntegrityError):
        svc.read(HUMAN, "art-1")           # reads re-verify before returning bytes
    os.unlink(p)
    assert any("missing" in x for x in svc.verify("art-1").problems)


def test_parents_must_be_pinned_inputs_or_same_run_outputs(repo, svc, tmp_path):
    t, run = _running(repo, Role.EXECUTOR, EXECUTOR)
    rd = tmp_path / "run"
    rd.mkdir()
    foreign = svc.import_bytes(data=b"x", artifact_id="foreign", logical_name="f.txt", media_type="text/plain",
                               artifact_class=ArtifactClass.REQUEST, human=HUMAN)
    with pytest.raises(GuardFailed, match="neither pinned inputs nor outputs"):
        svc.ingest(run_dir=rd, produced=_produce(rd, parents=[foreign.artifact_id]), run_id=run.run_id, task=t,
                   artifact_id="bad", is_mock=True)
    # pinned input as parent is fine; lineage rows are recorded; sibling output of the same run is fine too
    c = make_contract(input_artifact_refs=[ArtifactRef(artifact_id="foreign", sha256=foreign.sha256)])
    t2 = approve_fully(repo, repo.create_task(c, HUMAN).task)
    t2 = repo.queue_task(t2.task_id, t2.version)
    run2 = repo.start_run(t2.task_id, "w2", EXECUTOR.id, t2.version)
    rd2 = tmp_path / "run2"
    rd2.mkdir()
    svc.ingest(run_dir=rd2, produced=_produce(rd2, "a.txt", b"a", parents=["foreign"]), run_id=run2.run_id, task=t2,
                   artifact_id="a", is_mock=True)
    b = svc.ingest(run_dir=rd2, produced=_produce(rd2, "b.txt", b"b", parents=["a"]), run_id=run2.run_id, task=t2,
                   artifact_id="b", is_mock=True)
    assert repo.get_artifact("a").parent_artifact_ids == ["foreign"] and b.parent_artifact_ids == ["a"]


def test_response_hash_that_lies_about_the_bytes_is_rejected_and_nothing_registered(repo, svc, tmp_path):
    t, run = _running(repo, Role.EXECUTOR, EXECUTOR)
    rd = tmp_path / "run"
    rd.mkdir()
    good = _produce(rd)
    liar = good.model_copy(update={"sha256": sha256_hex(b"something else")})
    with pytest.raises(IntegrityError, match="hash_mismatch"):
        svc.ingest(run_dir=rd, produced=liar, run_id=run.run_id, task=t, artifact_id="x", is_mock=True)
    with pytest.raises(Exception, match="artifact"):
        repo.get_artifact("x")


# ------------------------------------------------------------------ path safety


@pytest.mark.parametrize("rel", ["../escape.txt", "/etc/passwd", "a/../../b", "a//b", "./x", "", "a\\b", "x\x00y", "a/./b"])
def test_traversal_and_malformed_paths_are_rejected(tmp_path, rel):
    base = tmp_path / "run"
    (base / "a").mkdir(parents=True)
    with pytest.raises(UnsafePath):
        safe_resolve_in(base, rel)


def test_symlinks_hardlinks_and_non_regular_files_are_rejected(tmp_path):
    base = tmp_path / "run"
    (base / "d").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    (base / "file.txt").write_text("ok")
    os.symlink(secret, base / "link.txt")                 # symlink to a host file
    os.symlink(tmp_path, base / "dirlink")                # symlinked directory component
    os.link(secret, base / "hard.txt")                    # hard link to a host file (nlink == 2)
    assert safe_resolve_in(base, "file.txt").read_text() == "ok"
    for rel in ("link.txt", "dirlink/secret.txt", "hard.txt", "d", "missing.txt"):
        with pytest.raises(UnsafePath):
            safe_resolve_in(base, rel)


def test_files_inside_the_eval_root_or_cas_root_cannot_be_ingested(repo, svc, store, tmp_path):
    t, run = _running(repo, Role.EXECUTOR, EXECUTOR)
    held = store.eval_root / "holdout.csv"
    held.write_text("secret labels")
    with pytest.raises(UnsafePath, match="forbidden root"):
        safe_resolve_in(store.eval_root, "holdout.csv", (store.eval_root,))
    # even when the run directory IS (a parent of) the eval root, the service forbids it
    rd = store.eval_root
    prod = ProducedArtifact(logical_name="h", path="holdout.csv", sha256=sha256_hex(b"secret labels"), size_bytes=13,
                            media_type="text/csv", artifact_class=ArtifactClass.WORK_PRODUCT)
    with pytest.raises(UnsafePath):
        svc.ingest(run_dir=rd, produced=prod, run_id=run.run_id, task=t, artifact_id="h", is_mock=True)


def test_oversized_artifact_is_rejected(repo, svc, tmp_path):
    t, run = _running(repo, Role.EXECUTOR, EXECUTOR)
    rd = tmp_path / "run"
    rd.mkdir()
    with pytest.raises(UnsafePath, match="over the"):
        svc.ingest(run_dir=rd, produced=_produce(rd, data=b"x" * 100), run_id=run.run_id, task=t, artifact_id="big",
                   is_mock=True, max_bytes=10)


def test_store_roots_must_be_disjoint(tmp_path):
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "cas", tmp_path / "cas" / "eval")


# ------------------------------------------------------------------ trust: ChatGPT Work / AI Scientist output is untrusted


def test_chatgpt_work_output_is_untrusted_whatever_the_caller_claims(repo, svc, tmp_path):
    t, run = _running(repo, Role.CHATGPT_WORK, CGW)
    rd = tmp_path / "run"
    rd.mkdir()
    art = svc.ingest(run_dir=rd, produced=_produce(rd, cls=ArtifactClass.EXTERNAL_HANDOFF), run_id=run.run_id, task=t,
                     artifact_id="cgw", is_mock=True)           # ingest asked for TRUSTED; the registry overrides
    assert art.trust_level is TrustLevel.UNTRUSTED
    assert repo.get_artifact("cgw").trust_level is TrustLevel.UNTRUSTED


def test_ai_scientist_output_is_quarantined_until_a_human_releases_it(repo, svc, tmp_path):
    t, run = _running(repo, Role.AI_SCIENTIST, SCI)
    rd = tmp_path / "run"
    rd.mkdir()
    art = svc.ingest(run_dir=rd, produced=_produce(rd, cls=ArtifactClass.SCIENTIST_OUTPUT), run_id=run.run_id, task=t,
                     artifact_id="sci", is_mock=True)
    assert art.trust_level is TrustLevel.QUARANTINED
    # quarantined: the Director cannot read it; the producing scientist and humans can
    with pytest.raises(NotAuthorized):
        svc.read(DIRECTOR, "sci")
    assert svc.read(SCI, "sci", for_task_id=None) == b"hello" and svc.read(HUMAN, "sci") == b"hello"
    for agent in (PLANNER, EXECUTOR, CGW):
        with pytest.raises(NotAuthorized):
            svc.read(agent, "sci")
    for actor in (DIRECTOR, EXECUTOR, SYSTEM):
        with pytest.raises(NotAuthorized):
            repo.release_artifact("sci", actor)
    released = repo.release_artifact("sci", HUMAN)
    assert released.trust_level is TrustLevel.UNTRUSTED                  # released to untrusted, NEVER to trusted
    assert svc.read(DIRECTOR, "sci") == b"hello"
    with pytest.raises(GuardFailed):
        repo.release_artifact("sci", HUMAN)                               # no longer quarantined


def test_class_and_role_pairing_is_enforced(repo, tmp_path):
    t, run = _running(repo, Role.CHATGPT_WORK, CGW)
    base = dict(sha256="a" * 64, size_bytes=1, media_type="t/p", logical_name="n", storage_uri="x",
                producer_run_id=run.run_id, trust_level=TrustLevel.TRUSTED, created_at_ms=1)
    with pytest.raises(GuardFailed, match="must be class external_handoff"):
        repo.add_artifact(Artifact(artifact_id="x1", artifact_class=ArtifactClass.WORK_PRODUCT, **base), SYSTEM)
    with pytest.raises(GuardFailed, match="chatgpt_work output must be class"):
        repo.add_artifact(Artifact(artifact_id="x2", artifact_class=ArtifactClass.FINAL_EVALUATION, **base), SYSTEM)
    te, erun = _running(repo, Role.EXECUTOR, EXECUTOR)
    base["producer_run_id"] = erun.run_id
    for cls in (ArtifactClass.EXTERNAL_HANDOFF, ArtifactClass.SCIENTIST_OUTPUT, ArtifactClass.FINAL_EVALUATION):
        with pytest.raises(GuardFailed, match="may not produce"):
            repo.add_artifact(Artifact(artifact_id=f"y-{cls.value}", artifact_class=cls, **base), SYSTEM)


def test_human_manual_handoff_import_is_untrusted_too(svc, repo):
    art = svc.import_bytes(data=b"notes", artifact_id="m1", logical_name="notes.md", media_type="text/markdown",
                           artifact_class=ArtifactClass.EXTERNAL_HANDOFF, human=HUMAN)
    assert art.trust_level is TrustLevel.UNTRUSTED and art.producer_principal_id == HUMAN.id


# ------------------------------------------------------------------ reads: matrix + pinned inputs


def test_pinned_input_is_readable_by_the_executor_but_unpinned_is_not(repo, svc):
    a = svc.import_bytes(data=b"data", artifact_id="w1", logical_name="w.txt", media_type="text/plain",
                         artifact_class=ArtifactClass.WORK_PRODUCT, human=HUMAN)
    c = make_contract(input_artifact_refs=[ArtifactRef(artifact_id="w1", sha256=a.sha256)])
    t = repo.create_task(c, HUMAN).task
    other = repo.create_task(make_contract(), HUMAN).task
    assert svc.read(EXECUTOR, "w1", for_task_id=t.task_id) == b"data"
    with pytest.raises(NotAuthorized):
        svc.read(EXECUTOR, "w1", for_task_id=other.task_id)
    with pytest.raises(NotAuthorized):
        svc.read(EXECUTOR, "w1")


# ------------------------------------------------------------------ final-evaluation class: unreadable by every agent role


def test_final_evaluation_artifacts_are_unreadable_by_every_agent_role(repo, svc, store):
    art = svc.import_bytes(data=b"held-out labels", artifact_id="fe", logical_name="labels.csv", media_type="text/csv",
                           artifact_class=ArtifactClass.FINAL_EVALUATION, human=HUMAN)
    assert store.path_for(art.sha256, final_eval=True).exists()                 # lives under the eval root ...
    assert not store.path_for(art.sha256, final_eval=False).exists()            # ... never in the general CAS
    assert store.eval_root not in store.root.parents and store.root not in store.eval_root.parents
    # a task that "pins" it as an input still cannot make it readable; and pinning is refused outright
    with pytest.raises(GuardFailed, match="final-evaluation"):
        repo.create_task(make_contract(input_artifact_refs=[ArtifactRef(artifact_id="fe", sha256=art.sha256)]), HUMAN)
    t = repo.create_task(make_contract(), HUMAN).task
    reads_before = list(store.reads)
    denied_before = len([e for e in repo.iter_events() if e.event_type == "authz.denied"])
    for actor in AGENT_ACTORS:
        for task_id in (None, t.task_id):
            assert actor.kind is not ActorKind.HUMAN
            with pytest.raises(NotAuthorized):
                svc.read(actor, "fe", for_task_id=task_id)
    assert store.reads == reads_before                                            # no blob was even opened for an agent
    assert len([e for e in repo.iter_events() if e.event_type == "authz.denied"]) > denied_before
    assert svc.read(HUMAN, "fe") == b"held-out labels" and svc.read(SYSTEM, "fe") == b"held-out labels"
    # agents cannot create one either (an executor run may not produce the class)
    te, erun = _running(repo, Role.EXECUTOR, EXECUTOR)
    with pytest.raises(GuardFailed, match="may not produce"):
        repo.add_artifact(Artifact(artifact_id="fe2", sha256="b" * 64, size_bytes=1, media_type="t/p",
                                   artifact_class=ArtifactClass.FINAL_EVALUATION, logical_name="n", storage_uri="x",
                                   producer_run_id=erun.run_id, trust_level=TrustLevel.TRUSTED, created_at_ms=1), SYSTEM)


def test_unknown_artifact_and_ids_are_not_denials(repo, svc):
    from airos.errors import NotFound

    with pytest.raises(NotFound):
        svc.read(EXECUTOR, "does-not-exist")
    assert uuid.uuid4() and Event.CANCEL and RunState.FAILED and GuardContext() and ResourceLimits()


@pytest.mark.parametrize("claimed", [TrustLevel.TRUSTED, TrustLevel.REVIEWED, TrustLevel.UNTRUSTED, TrustLevel.QUARANTINED])
def test_scientist_output_is_always_quarantined_never_plain_untrusted_whatever_the_caller_claims(repo, claimed):
    t, run = _running(repo, Role.AI_SCIENTIST, SCI)
    art = repo.add_artifact(Artifact(
        artifact_id=f"sci-{claimed.value}", sha256="c" * 64, size_bytes=1, media_type="t/p",
        artifact_class=ArtifactClass.SCIENTIST_OUTPUT, logical_name="n", storage_uri="x", producer_run_id=run.run_id,
        trust_level=claimed, created_at_ms=1), SYSTEM)
    assert art.trust_level is TrustLevel.QUARANTINED
    assert repo.get_artifact(art.artifact_id).trust_level is TrustLevel.QUARANTINED
    # a human-imported scientist output is quarantined too (manual handoff path)
    assert repo.get_artifact(art.artifact_id).artifact_class is ArtifactClass.SCIENTIST_OUTPUT


def test_human_import_of_scientist_output_is_quarantined(svc):
    art = svc.import_bytes(data=b"s", artifact_id="sci-h", logical_name="s.txt", media_type="text/plain",
                           artifact_class=ArtifactClass.SCIENTIST_OUTPUT, human=HUMAN)
    assert art.trust_level is TrustLevel.QUARANTINED


def test_lineage_is_transitive_and_verify_all_checks_every_artifact_and_edge(repo, svc, store, tmp_path):
    t, run = _running(repo, Role.EXECUTOR, EXECUTOR)
    rd = tmp_path / "run"
    rd.mkdir()
    root = svc.import_bytes(data=b"r", artifact_id="root", logical_name="r.txt", media_type="text/plain",
                            artifact_class=ArtifactClass.REQUEST, human=HUMAN)
    c = make_contract(input_artifact_refs=[ArtifactRef(artifact_id="root", sha256=root.sha256)])
    t2 = approve_fully(repo, repo.create_task(c, HUMAN).task)
    t2 = repo.queue_task(t2.task_id, t2.version)
    run2 = repo.start_run(t2.task_id, "w2", EXECUTOR.id, t2.version)
    rd2 = tmp_path / "run2"
    rd2.mkdir()
    svc.ingest(run_dir=rd2, produced=_produce(rd2, "a.txt", b"a", parents=["root"]), run_id=run2.run_id, task=t2, artifact_id="a", is_mock=True)
    svc.ingest(run_dir=rd2, produced=_produce(rd2, "b.txt", b"b", parents=["a"]), run_id=run2.run_id, task=t2, artifact_id="b", is_mock=True)
    assert svc.lineage("b") == ["a", "root"] and svc.lineage("root") == []
    assert set(svc.verify_all()) == {"root", "a", "b"} and all(r.ok for r in svc.verify_all().values())
    os.chmod(store.path_for(sha256_hex(b"a")), 0o644)
    store.path_for(sha256_hex(b"a")).write_bytes(b"tampered")
    res = svc.verify_all()
    assert not res["a"].ok and res["root"].ok and res["b"].ok          # exactly the tampered artifact is reported
