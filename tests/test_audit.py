from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from airos.audit import GENESIS_HASH, compute_entry_hash, verify_rows
from airos.domain.states import Event
from airos.errors import GuardFailed
from tests.conftest import DIRECTOR, HUMAN, PLANNER, approve_fully, make_contract, to_running

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
        assert repo.list_ledger()  # _populate queued/started a task, so reservation rows exist
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


def _anchored_repo(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    approve_fully(repo, t)  # two human grants -> two anchors
    assert repo.verify_chain_with_anchors().ok
    return t


def test_anchor_verification_passes_on_untouched_log(repo):
    _anchored_repo(repo)
    v = repo.verify_chain_with_anchors()
    assert v.ok and v.anchors_checked == 2 and v.problems == []


def test_anchor_verification_detects_tail_truncation(repo, db_path):
    """The chain alone cannot see truncation (it stays internally valid); the anchor file does."""
    _anchored_repo(repo)
    last_anchor_seq = int(repo.anchor_path.read_text().strip().splitlines()[-1].split("\t")[1])
    raw = _raw(db_path)
    raw.execute("DROP TRIGGER task_events_no_delete")
    raw.execute("DELETE FROM task_events WHERE seq >= ?", (last_anchor_seq,))
    raw.close()
    assert repo.verify_chain().ok                       # internally consistent shorter chain ...
    v = repo.verify_chain_with_anchors()
    assert not v.ok and any("truncated" in p and str(last_anchor_seq) in p for p in v.problems)


def test_anchor_verification_detects_whole_chain_rewrite_with_different_head(repo, db_path):
    """Attacker rewrites event 1 and recomputes EVERY later hash: chain is valid, head differs, anchors expose it."""
    from airos.audit import entry_material

    _anchored_repo(repo)
    old_head = repo.head()
    raw = _raw(db_path)
    raw.row_factory = sqlite3.Row
    raw.execute("DROP TRIGGER task_events_no_update")
    rows = [dict(r) for r in raw.execute("SELECT * FROM task_events ORDER BY seq")]
    rows[0]["payload_json"] = '{"rewritten":true}'
    prev = rows[0]["prev_hash"]
    for r in rows:
        r["prev_hash"] = prev
        r["entry_hash"] = compute_entry_hash(prev, entry_material(r))
        prev = r["entry_hash"]
        raw.execute("UPDATE task_events SET payload_json=?, prev_hash=?, entry_hash=? WHERE seq=?",
                    (r["payload_json"], r["prev_hash"], r["entry_hash"], r["seq"]))
    raw.close()
    assert repo.verify_chain().ok                        # a perfectly consistent forged chain ...
    assert repo.head().entry_hash != old_head.entry_hash  # ... with a different head
    v = repo.verify_chain_with_anchors()
    assert not v.ok and any("rewritten" in p for p in v.problems)


def test_anchor_file_malformed_line_is_a_problem_not_ignored(repo):
    _anchored_repo(repo)
    with open(repo.anchor_path, "a") as f:
        f.write("garbage line\n")
    v = repo.verify_chain_with_anchors()
    assert not v.ok and any("malformed" in p for p in v.problems)


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
    from airos.domain.states import RunState
    from tests.conftest import EXECUTOR
    t = repo.queue_task(t.task_id, t.version)
    run = repo.start_run(t.task_id, "w1", EXECUTOR.id, t.version)
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


def test_anchor_written_on_root_objective_completion(repo, tmp_path):
    from airos.domain.guards import GuardContext
    from airos.domain.schemas import Role, TaskKind
    from airos.domain.states import RunState
    from airos.persistence.sqlite_repo import SYSTEM
    from tests.conftest import EXECUTOR, free_contract

    anchor = tmp_path / "anchors" / "audit-anchors.log"
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
    child = repo.create_task(make_contract(), PLANNER, root.task_id).task
    child = approve_fully(repo, child)
    child = repo.queue_task(child.task_id, child.version)
    run = repo.start_run(child.task_id, "w1", EXECUTOR.id, child.version)
    child = repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    child = repo.transition(child.task_id, Event.ACCEPT, DIRECTOR, child.version)
    assert child.state.value == "completed"
    assert not anchor.exists() or all(ln.split("\t")[3] == "approval.granted" for ln in anchor.read_text().splitlines())
    n_before = len(anchor.read_text().splitlines())          # child completion is NOT a root completion
    root = repo.transition(root.task_id, Event.PLAN, PLANNER, root.version, GuardContext(plan_ok=True))
    root = repo.transition(root.task_id, Event.AUTO_APPROVE, SYSTEM, root.version)
    root = repo.queue_task(root.task_id, root.version)
    root = repo.transition(root.task_id, Event.START, SYSTEM, root.version)
    root = repo.transition(root.task_id, Event.FINISH, SYSTEM, root.version)
    assert len(anchor.read_text().splitlines()) == n_before  # reaching review is not completion either
    root = repo.transition(root.task_id, Event.ACCEPT, HUMAN, root.version)
    lines = anchor.read_text().splitlines()
    assert root.state.value == "completed" and len(lines) == n_before + 1
    assert lines[-1].split("\t")[3] == "task.transition" and lines[-1].split("\t")[2] == repo.head().entry_hash
    assert repo.verify_chain_with_anchors().ok
