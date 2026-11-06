from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from airos.audit import GENESIS_HASH, compute_entry_hash, verify_rows
from airos.domain.states import Event
from airos.errors import GuardFailed
from tests.conftest import DIRECTOR, HUMAN, approve_fully, make_contract, to_running

VECTORS = Path(__file__).parent / "vectors" / "audit_chain.json"


def _raw(db_path):
    return sqlite3.connect(db_path, isolation_level=None)


def _populate(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t, _ = to_running(repo, t)
    return t


def test_chain_is_gapless_linked_and_head_matches(repo):
    _populate(repo)
    events = list(repo.iter_events())
    assert len(events) > 5
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].prev_hash == GENESIS_HASH
    for a, b in zip(events, events[1:], strict=False):
        assert b.prev_hash == a.entry_hash
    head = repo.head()
    assert (head.seq, head.entry_hash) == (events[-1].seq, events[-1].entry_hash)
    v = repo.verify_chain()
    assert v.ok and v.checked == len(events) and v.broken_seq is None and v.head_hash == head.entry_hash


def test_every_state_change_has_matching_event(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    before = repo.head().seq
    t2 = repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version)
    new = [e for e in repo.iter_events(before + 1)]
    assert [(e.event_type, e.from_state, e.to_state) for e in new] == [("task.transition", "draft", "cancelled")]
    assert t2.state.value == "cancelled"


@pytest.mark.parametrize("table", ["task_events", "budget_ledger"])
def test_triggers_block_update_and_delete(repo, db_path, table):
    _populate(repo)
    if table == "budget_ledger":
        from airos.domain.schemas import BudgetLedgerEntry
        repo.append_ledger_entry(BudgetLedgerEntry(entry_id="e1", budget_id="b", kind="reserve", usd_micro=5,
                                                   idempotency_key="k1", created_at_ms=1), HUMAN)
    raw = _raw(db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute(f"UPDATE {table} SET {'payload_json' if table == 'task_events' else 'usd_micro'}=" +
                    ("'{}'" if table == "task_events" else "0"))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute(f"DELETE FROM {table}")
    raw.close()


def test_repository_exposes_no_mutation_api_for_audit():
    from airos.persistence.sqlite_repo import SqliteRepository

    public = {n for n in dir(SqliteRepository) if not n.startswith("_")}
    assert not {n for n in public if "event" in n and n.split("_")[0] in ("update", "delete", "remove", "edit")}
    assert {"head", "iter_events", "verify_chain"} <= public


def _tamper(repo, db_path, mutate):
    _populate(repo)
    assert repo.verify_chain().ok
    raw = _raw(db_path)
    raw.execute("DROP TRIGGER task_events_no_update")  # test-only: simulate an attacker with file access
    raw.execute("DROP TRIGGER task_events_no_delete")
    mutate(raw)
    raw.close()
    return repo.verify_chain()


def test_tamper_modified_payload_reports_exact_seq(repo, db_path):
    K = 4
    v = _tamper(repo, db_path, lambda raw: raw.execute(
        "UPDATE task_events SET payload_json='{\"forged\":true}' WHERE seq=?", (K,)))
    assert not v.ok and v.broken_seq == K and "altered" in v.reason


def test_tamper_modified_actor_reports_exact_seq(repo, db_path):
    K = 7
    v = _tamper(repo, db_path, lambda raw: raw.execute("UPDATE task_events SET actor_id='mallory' WHERE seq=?", (K,)))
    assert not v.ok and v.broken_seq == K


def test_tamper_deleted_row_reports_gap_at_exact_seq(repo, db_path):
    K = 5
    v = _tamper(repo, db_path, lambda raw: raw.execute("DELETE FROM task_events WHERE seq=?", (K,)))
    assert not v.ok and v.broken_seq == K and "gap" in v.reason


def test_tamper_rehashed_entry_breaks_next_link(repo, db_path):
    """Attacker edits entry K AND recomputes its entry_hash: the chain still breaks at K+1."""
    K = 6

    def mutate(raw):
        raw.row_factory = sqlite3.Row
        row = dict(raw.execute("SELECT * FROM task_events WHERE seq=?", (K,)).fetchone())
        row["payload_json"] = '{"forged":1}'
        from airos.audit import entry_material
        new_hash = compute_entry_hash(row["prev_hash"], entry_material(row))
        raw.execute("UPDATE task_events SET payload_json=?, entry_hash=? WHERE seq=?", (row["payload_json"], new_hash, K))

    v = _tamper(repo, db_path, mutate)
    assert not v.ok and v.broken_seq == K + 1 and "prev_hash" in v.reason


def test_tamper_truncated_tail_is_detected_via_anchor(repo, db_path, tmp_path):
    """Truncation leaves a valid shorter chain; only the external anchor exposes it (documented limit)."""
    t = repo.create_task(make_contract(), HUMAN).task
    t = approve_fully(repo, t)  # decisions append anchors
    anchor_lines = (tmp_path / "anchors" / "audit-anchors.log").read_text().strip().splitlines()
    last_anchor_seq = int(anchor_lines[-1].split("\t")[1])
    raw = _raw(db_path)
    raw.execute("DROP TRIGGER task_events_no_delete")
    raw.execute("DELETE FROM task_events WHERE seq > ?", (last_anchor_seq - 2,))
    raw.close()
    v = repo.verify_chain()
    assert v.ok  # chain alone cannot see truncation ...
    assert v.head_seq < last_anchor_seq  # ... but the anchor file proves the log is shorter than it was


def test_anchor_written_on_human_approval_and_root_completion_only(repo, tmp_path):
    anchor = tmp_path / "anchors" / "audit-anchors.log"
    t = repo.create_task(make_contract(), HUMAN).task
    assert not anchor.exists()  # creation is not an anchor point
    t = approve_fully(repo, t)
    lines = anchor.read_text().strip().splitlines()
    assert len(lines) == 2  # two approval grants (execute, cost)
    assert all(ln.split("\t")[3] == "approval.granted" for ln in lines)
    events = {e.seq: e for e in repo.iter_events()}
    for ln in lines:
        ts, seq, h, typ = ln.split("\t")
        assert events[int(seq)].entry_hash == h and events[int(seq)].ts_ms == int(ts)
    # complete the root task -> exactly one more anchor line
    from airos.domain.guards import GuardContext
    from airos.domain.states import RunState
    from airos.persistence.sqlite_repo import SYSTEM
    from tests.conftest import EXECUTOR
    t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    run = repo.start_run(t.task_id, "w1", EXECUTOR.id, t.version, GuardContext(budget_reserved=True))
    t = repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    repo.transition(t.task_id, Event.ACCEPT, DIRECTOR, t.version)
    lines = anchor.read_text().strip().splitlines()
    assert len(lines) == 3 and lines[-1].split("\t")[3] == "task.transition"
    assert lines[-1].split("\t")[2] == repo.head().entry_hash  # anchor == current head


def test_denial_is_also_anchored_and_failed_tx_writes_no_anchor(repo, tmp_path):
    anchor = tmp_path / "anchors" / "audit-anchors.log"
    t = repo.create_task(make_contract(), HUMAN).task
    from airos.domain.guards import GuardContext
    from airos.domain.schemas import ApprovalType
    from airos.persistence.sqlite_repo import SYSTEM
    from tests.conftest import PLANNER
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    repo.decide_approval(ap.approval_id, False, HUMAN, "too expensive")
    assert anchor.read_text().strip().splitlines()[-1].split("\t")[3] == "approval.denied"
    n = len(anchor.read_text().splitlines())
    with pytest.raises(GuardFailed):  # second decision on same approval fails and must not anchor
        repo.decide_approval(ap.approval_id, True, HUMAN)
    assert len(anchor.read_text().splitlines()) == n


def test_published_test_vectors_reproduce():
    data = json.loads(VECTORS.read_text())
    rows, prev = [], GENESIS_HASH
    for ev in data["events"]:
        row = {**ev, "payload_json": json.dumps(ev["payload"]), "prev_hash": prev}
        from airos.audit import entry_material
        h = compute_entry_hash(prev, entry_material(row))
        assert h == ev["expected_entry_hash"], ev["seq"]
        rows.append({**row, "entry_hash": h})
        prev = h
    assert verify_rows(rows).ok
    rows[1]["actor_id"] = "x"
    assert verify_rows(rows).broken_seq == 2
