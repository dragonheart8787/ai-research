from __future__ import annotations

import sqlite3
from pathlib import Path

from airos.domain.guards import GuardContext
from airos.domain.schemas import ApprovalType, ErrorRecord
from airos.domain.states import Event, RunState
from airos.persistence.sqlite_repo import SYSTEM
from airos.redaction import REDACTED, Redactor
from tests.conftest import EXECUTOR, HUMAN, PLANNER, make_contract

# Built at runtime so no secret-looking literal is committed to the repo.
FAKE_ENV_SECRET = "planted-" + "env-secret-" + "Zx9Qw7Lk3"
FAKE_ANTHROPIC = "sk-ant-" + "FAKEFAKEFAKEFAKE" + "1234567890"
FAKE_GH = "ghp_" + "FAKEFAKEFAKEFAKEFAKE" + "12345678"
ALL_FAKES = (FAKE_ENV_SECRET, FAKE_ANTHROPIC, FAKE_GH)


def test_redactor_unit():
    r = Redactor([FAKE_ENV_SECRET, "short"])
    text = f"a {FAKE_ENV_SECRET} b {FAKE_ANTHROPIC} c {FAKE_GH} short"
    out = r.redact_text(text)
    assert not any(s in out for s in ALL_FAKES) and out.count(REDACTED) == 3
    assert "short" in out  # < 8 chars is never registered
    nested = r.redact_json({"k" + FAKE_ENV_SECRET: [{"v": FAKE_GH}], "n": 5, "t": ("x", FAKE_ANTHROPIC)})
    assert FAKE_ENV_SECRET not in str(nested) and FAKE_GH not in str(nested) and nested["n"] == 5
    assert r.redact_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----") == REDACTED
    assert r.contains_secret(FAKE_GH) and not r.contains_secret("harmless text")


def _all_bytes(db_path: Path) -> bytes:
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            blob += p.read_bytes()
    return blob


def test_planted_secret_never_stored_anywhere(open_repo, db_path):
    repo = open_repo(redactor=Redactor([FAKE_ENV_SECRET]))
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True),
                        reason=f"planned using {FAKE_ENV_SECRET}")
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version, reason=f"token {FAKE_GH}")
    for req in t.contract.approval_requirements:
        ap = repo.request_approval(t.task_id, req.type, SYSTEM)
        repo.decide_approval(ap.approval_id, True, HUMAN, f"ok, key is {FAKE_ANTHROPIC}")
    t = repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)
    t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    run = repo.start_run(t.task_id, "w1", EXECUTOR.id, t.version, GuardContext(budget_reserved=True))
    err = ErrorRecord(ts_ms=1, attempt=1, error_class="adapter_error", retryable=True,
                      detail=f"HTTP 401 with Authorization: Bearer {FAKE_ANTHROPIC} and {FAKE_ENV_SECRET}")
    repo.complete_run(run.run_id, "w1", RunState.FAILED, retryable=True, error=err)
    assert repo.get_task(t.task_id).error_history  # the error was stored ...
    assert repo.verify_chain().ok                   # ... redacted *before* hashing, so the chain is valid
    repo.close()

    raw = sqlite3.connect(db_path)
    for (table,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in raw.execute(f"SELECT * FROM {table}"):  # noqa: S608 - table names come from sqlite_master
            joined = " ".join(str(c) for c in row)
            for s in ALL_FAKES:
                assert s not in joined, f"secret leaked into {table}"
    raw.close()
    blob = _all_bytes(db_path)
    for s in ALL_FAKES:
        assert s.encode() not in blob, "secret present in raw database/WAL bytes"
    assert REDACTED.encode() in blob


def test_pattern_secret_redacted_even_when_not_registered(repo, db_path):
    t = repo.create_task(make_contract(), HUMAN).task
    repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version, reason=f"leaked {FAKE_ANTHROPIC}")
    assert all(FAKE_ANTHROPIC not in e.model_dump_json() for e in repo.iter_events())
    assert repo.verify_chain().ok


def test_redactor_from_environ(monkeypatch):
    monkeypatch.setenv("AIROS_TEST_SECRET", FAKE_ENV_SECRET)
    r = Redactor.from_environ(["AIROS_TEST_SECRET", "AIROS_MISSING"])
    assert FAKE_ENV_SECRET not in r.redact_text(f"x {FAKE_ENV_SECRET}")
    assert ApprovalType.COST  # keep import used
