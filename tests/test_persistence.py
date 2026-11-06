from __future__ import annotations

import sqlite3

import pytest

from airos.domain.states import Event, RunState, TaskState
from airos.errors import SchemaMismatch
from airos.persistence.sqlite_repo import SqliteRepository
from tests.conftest import HUMAN, make_contract, to_running


def test_state_survives_close_and_reopen(open_repo, db_path, clock):
    repo = open_repo()
    t = repo.create_task(make_contract(), HUMAN).task
    t, run_id = to_running(repo, t)
    ap_before = repo.list_approvals(t.task_id)
    head_before = repo.head()
    task_before = repo.get_task(t.task_id)
    runs_before = repo.list_runs(t.task_id)
    repo.close()

    reopened = SqliteRepository(db_path, clock=clock)
    assert reopened.get_task(t.task_id) == task_before
    assert reopened.list_runs(t.task_id) == runs_before
    assert reopened.list_approvals(t.task_id) == ap_before
    assert reopened.head() == head_before
    assert reopened.verify_chain().ok
    # and it is still operable: finish the run through the reopened repository
    run = reopened.list_runs(t.task_id)[0]
    # lease owner "w1" still valid because the fake clock did not advance
    out = reopened.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    assert out.state is TaskState.REVIEW
    reopened.close()


def test_pragmas_and_wal(repo, db_path):
    c = repo._conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_migration_checksum_is_enforced(open_repo, db_path):
    repo = open_repo()
    repo.close()
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("UPDATE schema_migrations SET sha256='deadbeef' WHERE version=1")

    raw.close()
    with pytest.raises(SchemaMismatch, match="modified"):
        SqliteRepository(db_path)


def test_reopen_detects_tampered_transition_table(open_repo, db_path):
    repo = open_repo()
    repo.close()
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("INSERT INTO allowed_transitions VALUES('completed','draft')")

    raw.close()
    with pytest.raises(SchemaMismatch, match="transition"):
        SqliteRepository(db_path)


def test_reopen_is_idempotent_for_migrations(open_repo, db_path):
    open_repo().close()
    r = SqliteRepository(db_path)
    n = r._conn().execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert n == 1
    r.close()


def test_task_persisted_in_every_state_reachable_by_flow(open_repo, db_path, clock):
    repo = open_repo()
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version)
    repo.close()
    again = SqliteRepository(db_path, clock=clock)
    assert again.get_task(t.task_id).state is TaskState.CANCELLED
    again.close()
