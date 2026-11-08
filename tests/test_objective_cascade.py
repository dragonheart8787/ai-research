"""A failed objective is cascaded by the SYSTEM actor only: live descendants are cancelled, running children lose
their lease and settle at worst case, queued ones release, and every event names the causal failing task."""
from __future__ import annotations

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.guards import GuardContext
from airos.domain.schemas import ExpectedOutput, ResourceLimits, Role, TaskKind
from airos.domain.states import BlockReason, Event, RunState, TaskState
from airos.errors import ActorNotAllowed, GuardFailed, LeaseLost, NotAuthorized
from airos.persistence import budget_sql
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import (
    AGENT_ACTORS,
    DIRECTOR,
    EXECUTOR,
    HUMAN,
    PLANNER,
    approve_fully,
    free_contract,
    make_contract,
)

USD = 1_000_000


def _running_objective(repo):
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
    return root


def _start_objective(repo, root):
    t = repo.transition(root.task_id, Event.PLAN, PLANNER, root.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.AUTO_APPROVE, SYSTEM, t.version)
    t = repo.queue_task(t.task_id, t.version)
    return repo.transition(t.task_id, Event.START, SYSTEM, t.version)


def _tree(repo):
    root = _running_objective(repo)
    kid = lambda **kw: repo.create_task(make_contract(**kw), PLANNER, root.task_id).task  # noqa: E731
    running = approve_fully(repo, kid())
    running = repo.queue_task(running.task_id, running.version)
    run = repo.start_run(running.task_id, "w1", EXECUTOR.id, running.version)
    queued = repo.queue_task(*(lambda t: (t.task_id, t.version))(approve_fully(repo, kid())))
    approved = approve_fully(repo, kid())
    draft = kid()
    blocked = approve_fully(repo, kid())
    repo.set_budget(f"root:{root.task_id}-x", "root", "x", 0, 0, HUMAN)  # unrelated budget row; keeps set_budget exercised
    blocked = repo.transition(blocked.task_id, Event.BLOCK, SYSTEM, blocked.version,
                              GuardContext(block_reason=BlockReason.NEEDS_INPUT))
    done = approve_fully(repo, kid())
    done = repo.queue_task(done.task_id, done.version)
    drun = repo.start_run(done.task_id, "w2", EXECUTOR.id, done.version)
    done = repo.complete_run(drun.run_id, "w2", RunState.SUCCEEDED, verifier_passed=True)
    done = repo.transition(done.task_id, Event.ACCEPT, DIRECTOR, done.version)
    grandchild = repo.create_task(make_contract(), PLANNER, queued.task_id).task
    return root, dict(running=running, queued=queued, approved=approved, draft=draft, blocked=blocked, done=done,
                      grandchild=grandchild), run


def test_failed_objective_cancels_live_descendants_with_causal_audit_and_consistent_ledger(repo):
    root, kids, run = _tree(repo)
    root = _start_objective(repo, root)
    head_before = repo.head().seq
    failed = repo.transition(root.task_id, Event.FAIL, SYSTEM, root.version, reason="child failed")
    assert failed.state is TaskState.FAILED
    states = {k: repo.get_task(t.task_id).state for k, t in kids.items()}
    assert states == {"running": TaskState.CANCELLED, "queued": TaskState.CANCELLED, "approved": TaskState.CANCELLED,
                      "draft": TaskState.CANCELLED, "blocked": TaskState.CANCELLED, "done": TaskState.COMPLETED,
                      "grandchild": TaskState.CANCELLED}
    # audit: SYSTEM actor, cascade_cancel event, linked to the causal failing task
    events = [e for e in repo.iter_events(head_before + 1) if e.payload.get("event") == "cascade_cancel"]
    assert len(events) == 6 and {e.task_id for e in events} == {t.task_id for k, t in kids.items() if k != "done"}
    for e in events:
        assert (e.actor_kind, e.actor_id) == ("system", "system")
        assert e.payload["cause_task_id"] == root.task_id and e.payload["cause_event"] == "fail"
    # running child lost its lease
    r = repo.list_runs(kids["running"].task_id)[0]
    assert r.state is RunState.CANCELLED and repo.heartbeat(r.run_id, "w1") is False
    with pytest.raises(LeaseLost):
        repo.complete_run(r.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True)
    # ledger: running child settled at WORST CASE (full reservation), queued child released with no charge
    assert repo.budget_status(f"task:{kids['running'].task_id}").used_usd_micro == USD
    assert repo.budget_status(f"task:{kids['queued'].task_id}").used_usd_micro == 0
    ledger = repo.list_ledger()
    reserves = {e.entry_id for e in ledger if e.kind.value == "reserve"}
    assert reserves == {e.reserve_ref for e in ledger if e.kind.value == "release"}          # nothing left reserved
    settles = [e for e in ledger if e.kind.value == "settle" and e.task_id == kids["running"].task_id]
    assert settles and all(e.usd_micro == USD for e in settles)
    assert not [e for e in ledger if e.kind.value == "settle" and e.task_id == kids["queued"].task_id]
    assert repo.budget_status(f"root:{root.task_id}").used_usd_micro == 2 * USD  # worst case of the cancelled running child + the completed child (no measured usage => worst case)
    assert repo.verify_chain().ok


def test_cascade_is_idempotent_and_leaves_ledger_sums_unchanged(repo):
    root, kids, _ = _tree(repo)
    root = _start_objective(repo, root)
    repo.transition(root.task_id, Event.FAIL, SYSTEM, root.version)
    snap = ([e.entry_id for e in repo.list_ledger()], {k: repo.get_task(t.task_id).state for k, t in kids.items()})
    head = repo.head()
    assert repo.cascade_cancel_descendants(root.task_id) == []      # first explicit call: nothing left to do
    assert repo.cascade_cancel_descendants(root.task_id) == []      # and again
    assert ([e.entry_id for e in repo.list_ledger()], {k: repo.get_task(t.task_id).state for k, t in kids.items()}) == snap
    assert repo.head().seq == head.seq or all(e.event_type != "task.transition" for e in repo.iter_events(head.seq + 1))


def test_explicit_cascade_repairs_a_failed_parent_with_live_children(repo, db_path):
    """If a parent ended without its cascade (simulated at the SQL level), the explicit system call finishes the job once."""
    import sqlite3

    root = _running_objective(repo)
    child = repo.create_task(make_contract(), PLANNER, root.task_id).task
    repo.transition(root.task_id, Event.CANCEL, HUMAN, root.version)          # cascades normally; undo it for the simulation
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("DROP TRIGGER tasks_transition_guard")
    raw.execute("DROP TRIGGER tasks_version_monotonic")
    raw.execute("UPDATE tasks SET state='draft' WHERE task_id=?", (child.task_id,))
    raw.close()
    assert repo.get_task(child.task_id).state is TaskState.DRAFT
    assert repo.cascade_cancel_descendants(root.task_id) == [child.task_id]
    assert repo.cascade_cancel_descendants(root.task_id) == []


def test_cascade_requires_an_ended_parent(repo):
    root = _start_objective(repo, _running_objective(repo))
    with pytest.raises(GuardFailed, match="only an ended task cascades"):
        repo.cascade_cancel_descendants(root.task_id)


@pytest.mark.parametrize("actor", [*AGENT_ACTORS, HUMAN], ids=lambda a: a.kind.value)
def test_no_agent_action_can_trigger_the_cascade_directly(repo, actor):
    root, kids, _ = _tree(repo)
    root = _start_objective(repo, root)
    before = {k: repo.get_task(t.task_id).state for k, t in kids.items()}
    for ev in (Event.FAIL, Event.TIMEOUT, Event.CASCADE_CANCEL, Event.CANCEL):
        if actor.kind.value == "human" and ev is Event.CANCEL:
            continue  # a HUMAN may cancel (that is a different, human-authorised cascade)
        with pytest.raises(ActorNotAllowed):
            repo.transition(root.task_id, ev, actor, root.version, GuardContext(cascade=True))
    with pytest.raises(NotAuthorized):
        repo.cascade_cancel_descendants(root.task_id, actor=actor)
    assert {k: repo.get_task(t.task_id).state for k, t in kids.items()} == before
    assert repo.get_task(root.task_id).state is TaskState.RUNNING


def test_matrix_has_a_system_only_cascade_row():
    from airos.domain.states import ActorKind
    from airos.security import permissions

    assert permissions.ACTIONS["cascade.cancel"] == {ActorKind.SYSTEM}
    assert permissions.ACTIONS["event:cascade_cancel"] == {ActorKind.SYSTEM}


def test_objective_timeout_also_cascades(repo):
    root, kids, _ = _tree(repo)
    root = _start_objective(repo, root)
    repo.transition(root.task_id, Event.TIMEOUT, SYSTEM, root.version)
    assert repo.get_task(kids["queued"].task_id).state is TaskState.CANCELLED
    ev = [e for e in repo.iter_events() if e.payload.get("cause_event") == "timeout"]
    assert ev and all(e.payload["cause_task_id"] == root.task_id for e in ev)


def test_worker_fail_fast_cancels_siblings_end_to_end(repo, tmp_path):
    adapters = all_mock_adapters()
    adapters[Role.EXECUTOR] = MockAdapter(Role.EXECUTOR, script=lambda r: "permanent_failure")
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], adapters, {})
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w")
    root = _start_objective(repo, _running_objective(repo))
    out = [ExpectedOutput(logical_name="o.txt", media_type="text/plain", artifact_class="work_product")]
    a = approve_fully(repo, repo.create_task(make_contract(expected_output=out), PLANNER, root.task_id).task)
    b = approve_fully(repo, repo.create_task(make_contract(expected_output=out, resource_limits=ResourceLimits(
        max_usd_micro=USD, max_retries=2)), PLANNER, root.task_id).task)
    for _ in range(4):
        w.tick()
    states = {repo.get_task(x.task_id).state for x in (a, b)}
    assert repo.get_task(root.task_id).state is TaskState.FAILED
    assert TaskState.FAILED in states and states <= {TaskState.FAILED, TaskState.CANCELLED}
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve"} == {e.reserve_ref for e in ledger if e.kind.value == "release"}
    assert budget_sql.global_budget_id(repo.clock.now_ms())
