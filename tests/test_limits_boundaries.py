"""Limits: each enforced at the boundary — the limit itself is allowed, limit+1 is rejected."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ResourceLimits, Role, TaskKind
from airos.domain.states import Event, RunState, TaskState
from airos.errors import GuardFailed, LimitExceeded
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import (
    DIRECTOR,
    EXECUTOR,
    HUMAN,
    PLANNER,
    approve_fully,
    free_contract,
    make_contract,
    to_running,
)

D = Limits()  # the documented defaults


def test_documented_defaults():
    assert (D.max_subtask_depth, D.max_children_per_task, D.max_total_tasks_per_root, D.max_review_cycles) == (3, 8, 25, 2)


def _root(repo):
    return repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task


def _child(repo, parent):
    return repo.create_task(free_contract(), PLANNER, parent.task_id).task


# ------------------------------------------------------------------ retries


def test_hard_cap_on_max_retries():
    assert ResourceLimits(max_retries=5).max_retries == 5
    with pytest.raises(ValidationError):
        ResourceLimits(max_retries=6)


@pytest.mark.parametrize("max_retries", [0, 1, 2, 5])
def test_retry_boundary_max_retries_allowed_one_more_rejected(repo, clock, max_retries):
    t = repo.create_task(make_contract(resource_limits=ResourceLimits(max_usd_micro=1_000, max_retries=max_retries)), HUMAN).task
    t = approve_fully(repo, t)
    t = repo.queue_task(t.task_id, t.version)
    attempts = 0
    while True:
        clock.advance(10_000_000)                                  # past any backoff
        run = repo.start_run(t.task_id, "w", EXECUTOR.id, t.version)
        attempts += 1
        if attempts == 1 + max_retries:                            # the (max_retries+1)-th failure: a retry is rejected
            with pytest.raises(GuardFailed, match="retries exhausted"):
                repo.transition(t.task_id, Event.RETRY, SYSTEM, repo.get_task(t.task_id).version,
                                GuardContext(error_retryable=True))
        t = repo.complete_run(run.run_id, "w", RunState.FAILED, retryable=True)
        if t.state is not TaskState.QUEUED:
            break
    assert attempts == 1 + max_retries                              # max_retries RETRIES were allowed ...
    assert t.state is TaskState.FAILED                              # ... the next one (limit+1) was rejected
    assert t.attempt_count == 1 + max_retries


def test_review_cycle_boundary(repo):
    t = repo.create_task(make_contract(resource_limits=ResourceLimits(max_usd_micro=1_000, max_retries=5)), HUMAN).task
    t, run_id = to_running(repo, t)
    worker = "w1"
    for cycle in range(D.max_review_cycles):                         # 2 reworks allowed
        t = repo.complete_run(run_id, worker, RunState.SUCCEEDED, verifier_passed=True)
        t = repo.enqueue(t.task_id, Event.REWORK, DIRECTOR, t.version)
        assert t.review_cycles == cycle + 1
        run_id = repo.start_run(t.task_id, worker, EXECUTOR.id, t.version).run_id
    t = repo.complete_run(run_id, worker, RunState.SUCCEEDED, verifier_passed=True)
    with pytest.raises(GuardFailed, match="review cycles exhausted"):  # the 3rd (limit+1) is rejected
        repo.enqueue(t.task_id, Event.REWORK, DIRECTOR, t.version)
    assert repo.get_task(t.task_id).state is TaskState.REVIEW


# ------------------------------------------------------------------ recursion


def test_subtask_depth_boundary(repo):
    cur = _root(repo)
    assert cur.depth == 0
    for depth in range(1, D.max_subtask_depth + 1):                  # depth 1, 2, 3 allowed
        cur = _child(repo, cur)
        assert cur.depth == depth
    with pytest.raises(LimitExceeded, match="max_subtask_depth=3"):  # depth 4 rejected
        _child(repo, cur)


def test_children_per_task_boundary(repo):
    root = _root(repo)
    for _ in range(D.max_children_per_task):                         # 8 allowed
        _child(repo, root)
    with pytest.raises(LimitExceeded, match="max_children_per_task=8"):  # 9th rejected
        _child(repo, root)


def test_total_tasks_per_root_boundary(repo):
    root = _root(repo)                                               # 1
    kids = [_child(repo, root) for _ in range(8)]                    # 9
    for k in kids[:2]:
        for _ in range(8):                                           # +16 = 25 (the limit itself)
            _child(repo, k)
    n = repo._conn().execute("SELECT COUNT(*) FROM tasks WHERE root_task_id=?", (root.task_id,)).fetchone()[0]
    assert n == D.max_total_tasks_per_root == 25
    flags = repo._conn().execute("SELECT COUNT(*) FROM tasks WHERE root_task_id=? AND over_root_limit=1", (root.task_id,)).fetchone()[0]
    assert flags == 0                                                # up to and including the 25th: no approval needed
    twenty_sixth = _child(repo, kids[2])                             # limit+1: created, but BLOCKED behind a human approval
    assert twenty_sixth.over_root_limit is True
    assert _child(repo, _root(repo)).over_root_limit is False        # a different root is unaffected


def test_limits_cannot_be_exceeded_by_duplicate_creation_either(repo):
    root = _root(repo)
    c = free_contract()
    first = repo.create_task(c, PLANNER, root.task_id)
    again = repo.create_task(c, PLANNER, root.task_id)               # idempotent duplicate: no new row, no limit use
    assert first.created and not again.created
    n = repo._conn().execute("SELECT COUNT(*) FROM tasks WHERE root_task_id=?", (root.task_id,)).fetchone()[0]
    assert n == 2
