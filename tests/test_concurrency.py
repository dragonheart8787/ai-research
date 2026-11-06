from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from airos.domain.guards import GuardContext
from airos.domain.states import Event, TaskState
from airos.errors import IdempotencyConflict, VersionConflict
from tests.conftest import HUMAN, PLANNER, make_contract

N = 12


def _race(fns):
    barrier = threading.Barrier(len(fns))

    def wrap(fn):
        def run():
            barrier.wait()
            try:
                return ("ok", fn())
            except Exception as exc:  # noqa: BLE001
                return ("err", exc)

        return run

    with ThreadPoolExecutor(len(fns)) as pool:
        futs = [pool.submit(wrap(f)) for f in fns]
        return [f.result() for f in futs]


def test_same_transition_from_many_threads_exactly_one_wins(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    res = _race([lambda: repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version) for _ in range(N)])
    wins = [r for r in res if r[0] == "ok"]
    losses = [r[1] for r in res if r[0] == "err"]
    assert len(wins) == 1
    assert len(losses) == N - 1 and all(isinstance(e, VersionConflict) for e in losses)
    final = repo.get_task(t.task_id)
    assert final.state is TaskState.CANCELLED and final.version == t.version + 1
    assert repo.verify_chain().ok


def test_different_events_racing_on_same_version_exactly_one_wins(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    res = _race([
        lambda: repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version),
        lambda: repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True)),
    ])
    assert sorted(r[0] for r in res) == ["err", "ok"]
    assert isinstance(next(r[1] for r in res if r[0] == "err"), VersionConflict)


def test_concurrent_duplicate_creation_yields_one_row(repo):
    c = make_contract()
    res = _race([lambda: repo.create_task(c, HUMAN) for _ in range(N)])
    assert all(r[0] == "ok" for r in res), [r for r in res if r[0] == "err"]
    results = [r[1] for r in res]
    assert sum(1 for r in results if r.created) == 1
    assert len({r.task.task_id for r in results}) == 1
    assert len(repo.list_tasks()) == 1
    assert repo._conn().execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key=?", (c.idempotency_key,)).fetchone()[0] == 1
    assert repo.verify_chain().ok


def test_duplicate_create_returns_existing_never_second_row(repo):
    c = make_contract()
    first = repo.create_task(c, HUMAN)
    second = repo.create_task(c, HUMAN)
    assert first.created and not second.created
    assert first.task.task_id == second.task.task_id
    assert len(repo.list_tasks()) == 1


def test_same_key_different_contract_conflicts_and_is_audited(repo):
    c = make_contract()
    repo.create_task(c, HUMAN)
    other = make_contract(idempotency_key=c.idempotency_key, research_objective="something else")
    with pytest.raises(IdempotencyConflict):
        repo.create_task(other, HUMAN)
    assert len(repo.list_tasks()) == 1
    assert any(e.event_type == "task.idempotency_conflict" for e in repo.iter_events())


def test_db_unique_constraint_backs_idempotency(repo, db_path):
    import sqlite3

    c = make_contract()
    t = repo.create_task(c, HUMAN).task
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO tasks(task_id,idempotency_key,kind,root_task_id,depth,assigned_role,state,version,contract_version,"
            "contract_json,contract_sha256,side_effect_class,created_by,created_at_ms,updated_at_ms,state_entered_at_ms) "
            "VALUES('x',?, 'work','x',0,'executor','draft',1,1,'{}','h','none',?,1,1,1)", (c.idempotency_key, HUMAN.id))
    raw.close()
    assert t.task_id
