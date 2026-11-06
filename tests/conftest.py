from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from airos.clock import FakeClock
from airos.domain.guards import GuardContext
from airos.domain.schemas import (
    AcceptanceCriterion,
    ApprovalRequirement,
    ApprovalType,
    ResourceLimits,
    Role,
    TaskContract,
    TaskRecord,
)
from airos.domain.states import Actor, ActorKind, Event
from airos.persistence.sqlite_repo import SYSTEM, SqliteRepository

HUMAN = Actor(ActorKind.HUMAN, "human-1")
HUMAN2 = Actor(ActorKind.HUMAN, "human-2")
PLANNER = Actor(ActorKind.PLANNER, "planner-1")
DIRECTOR = Actor(ActorKind.DIRECTOR, "director-1")
EXECUTOR = Actor(ActorKind.EXECUTOR, "executor-1")

_counter = itertools.count(1)


def make_contract(**over: object) -> TaskContract:
    """A valid executor contract needing approvals (execute, cost) unless overridden."""
    n = next(_counter)
    base: dict[str, object] = {
        "idempotency_key": f"idem-key-{n:08d}-abcdef",
        "research_objective": "mock objective (no real research)",
        "assigned_role": Role.EXECUTOR,
        "acceptance_criteria": [AcceptanceCriterion(id="c1", description="has output", mode="reviewer")],
        "resource_limits": ResourceLimits(max_usd_micro=1_000_000, max_retries=2),
        "approval_requirements": [
            ApprovalRequirement(type=ApprovalType.EXECUTE, reason="executor"),
            ApprovalRequirement(type=ApprovalType.COST, reason="metered"),
        ],
        "allowed_actions": ["fs.read", "fs.write.workspace"],
    }
    base.update(over)
    return TaskContract(**base)  # type: ignore[arg-type]


def free_contract(**over: object) -> TaskContract:
    """Zero-cost, side-effect-free, non-executor task: auto-approvable."""
    return make_contract(
        assigned_role=Role.PLANNER,
        resource_limits=ResourceLimits(max_usd_micro=0, max_retries=2),
        approval_requirements=[],
        allowed_actions=[],
        **over,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "airos.db"


@pytest.fixture
def open_repo(db_path: Path, clock: FakeClock, tmp_path: Path) -> Iterator[Callable[..., SqliteRepository]]:
    made: list[SqliteRepository] = []

    def _open(**kw: object) -> SqliteRepository:
        kw.setdefault("clock", clock)
        kw.setdefault("anchor_path", tmp_path / "anchors" / "audit-anchors.log")
        r = SqliteRepository(db_path, **kw)  # type: ignore[arg-type]
        made.append(r)
        for a, t, role in (
            (HUMAN, "human", None), (HUMAN2, "human", None),
            (PLANNER, "agent", "planner"), (DIRECTOR, "agent", "director"), (EXECUTOR, "agent", "executor"),
        ):
            r.register_principal(a.id, t, role, a.id)
        return r

    yield _open
    for r in made:
        r.close()


@pytest.fixture
def repo(open_repo: Callable[..., SqliteRepository]) -> SqliteRepository:
    return open_repo()


def approve_fully(repo: SqliteRepository, task: TaskRecord) -> TaskRecord:
    """draft -> planned -> awaiting_approval -> (human grants all) -> approved."""
    t = repo.transition(task.task_id, Event.PLAN, PLANNER, task.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    for req in t.contract.approval_requirements:
        ap = repo.request_approval(t.task_id, req.type, SYSTEM)
        repo.decide_approval(ap.approval_id, True, HUMAN, "ok")
    return repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)


def to_running(repo: SqliteRepository, task: TaskRecord, worker: str = "w1") -> tuple[TaskRecord, str]:
    t = approve_fully(repo, task)
    t = repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    run = repo.start_run(t.task_id, worker, EXECUTOR.id, t.version, GuardContext(budget_reserved=True))
    return repo.get_task(t.task_id), run.run_id
