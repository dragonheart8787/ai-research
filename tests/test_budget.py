"""Budget service: reserve before queue, settle, release, hard limits at every level, human-gated increases."""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from airos.domain import machine
from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ApprovalType, ResourceLimits, ResourceUsage, Role, TaskKind
from airos.domain.states import ActorKind, BlockReason, Event, RunState, TaskState
from airos.errors import (
    AirosError,
    GuardFailed,
    NotAuthorized,
    StaleApproval,
)
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
    to_running,
)

USD = 1_000_000  # the default make_contract() per-attempt cap (1.00 USD)


def _approved(repo, **over):
    return approve_fully(repo, repo.create_task(make_contract(**over), HUMAN).task)


def _usage(usd=0, tin=0, tout=0):
    """USD is derived from tokens by the control plane (unit-model = 1 micro-USD/token); the reported usd is irrelevant."""
    return ResourceUsage(tokens_in=tin + usd, tokens_out=tout, usd_micro=usd, wall_ms=1, measured_by="adapter", model="unit-model")


def _sums(repo, budget_id):
    rows = [e for e in repo.list_ledger(budget_id)]
    return {k: sum(e.usd_micro for e in rows if e.kind.value == k) for k in ("reserve", "release", "settle", "grant")}


def _blocked_reason(repo, task_id):
    t = repo.get_task(task_id)
    return t.state, t.blocked_reason


# ------------------------------------------------------------------ reservation gate


def test_queue_task_reserves_at_task_root_and_global_levels(repo, clock):
    t = _approved(repo)
    q = repo.queue_task(t.task_id, t.version)
    assert q.state is TaskState.QUEUED
    rows = [e for e in repo.list_ledger() if e.kind.value == "reserve"]
    assert {e.budget_id for e in rows} == {
        f"task:{t.task_id}", f"root:{t.root_task_id}", budget_sql.global_budget_id(clock.now_ms())}
    assert all(e.usd_micro == USD and e.attempt == 1 and e.task_id == t.task_id for e in rows)
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == USD
    assert any(e.event_type == "budget.reserved" for e in repo.iter_events())


def test_direct_transition_to_queued_without_reservation_is_refused_by_guard(repo):
    t = _approved(repo)
    with pytest.raises(GuardFailed, match="no open ledger reservation"):
        repo.transition(t.task_id, Event.QUEUE, SYSTEM, t.version)
    assert repo.get_task(t.task_id).state is TaskState.APPROVED
    assert [e for e in repo.list_ledger() if e.task_id == t.task_id] == []


def test_every_transition_into_queued_or_running_declares_the_reservation_guard():
    entering = [t for t in machine.TRANSITIONS if t.to_state in (TaskState.QUEUED, TaskState.RUNNING)]
    assert {t.event for t in entering} == {Event.QUEUE, Event.UNBLOCK, Event.REWORK, Event.RETRY, Event.START}
    for t in entering:
        assert "reservation_held" in t.guards, t.tid


def test_database_refuses_queued_or_running_without_open_reservation_even_via_raw_sql(repo, db_path):
    t = _approved(repo)
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="open budget reservation"):
        raw.execute("UPDATE tasks SET state='queued' WHERE task_id=?", (t.task_id,))
    # with a reservation the same statement is legal; after it is RELEASED, running is refused again
    q = repo.queue_task(t.task_id, t.version)
    repo.transition(q.task_id, Event.CANCEL, HUMAN, q.version)  # releases the reservation
    assert budget_sql.open_task_reserve(repo._conn(), t.task_id, 1) is None
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("UPDATE tasks SET state='running' WHERE task_id=?", (t.task_id,))
    raw.close()


def test_no_code_path_reaches_queued_or_running_without_a_reservation(repo):
    """Try EVERY (event, actor) pair directly (no enqueue/reserve) on an approved task: never queued/running."""
    t = _approved(repo)
    ctx = GuardContext(plan_ok=True, verifier_passed=True, error_retryable=True, blocker_cleared=True,
                       block_reason=BlockReason.BUDGET_INSUFFICIENT, cascade=True)
    actors = [HUMAN, SYSTEM, *AGENT_ACTORS]
    for event in Event:
        for actor in actors:
            cur = repo.get_task(t.task_id)
            if cur.state.value in ("cancelled", "blocked"):
                break
            with contextlib.suppress(AirosError):
                repo.transition(t.task_id, event, actor, cur.version, ctx)
            assert repo.get_task(t.task_id).state not in (TaskState.QUEUED, TaskState.RUNNING), (event, actor)
    with pytest.raises(AirosError):  # and start_run cannot bypass the gate either
        repo.start_run(t.task_id, "w", EXECUTOR.id, repo.get_task(t.task_id).version)


def test_resuming_paths_also_need_a_fresh_reservation(repo):
    # blocked(budget) -> UNBLOCK directly (no reservation) is refused
    t = _approved(repo)
    repo.set_budget(f"root:{t.root_task_id}", "root", t.root_task_id, 0, 0, HUMAN)
    blocked = repo.queue_task(t.task_id, t.version)
    assert _blocked_reason(repo, blocked.task_id) == (TaskState.BLOCKED, BlockReason.BUDGET_INSUFFICIENT)
    with pytest.raises(GuardFailed, match="no open ledger reservation"):
        repo.transition(blocked.task_id, Event.UNBLOCK, HUMAN, blocked.version)
    # review -> REWORK directly is refused; running -> RETRY directly is refused
    t2, run_id = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    with pytest.raises(GuardFailed, match="no open ledger reservation|reservation"):
        repo.transition(t2.task_id, Event.RETRY, SYSTEM, t2.version, GuardContext(error_retryable=True))
    t2 = repo.complete_run(run_id, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_usage())
    with pytest.raises(GuardFailed, match="reservation"):
        repo.transition(t2.task_id, Event.REWORK, DIRECTOR, t2.version)
    reworked = repo.enqueue(t2.task_id, Event.REWORK, DIRECTOR, t2.version)  # the proper path reserves atomically
    assert reworked.state is TaskState.QUEUED and reworked.review_cycles == 1
    assert budget_sql.open_task_reserve(repo._conn(), t2.task_id, 2) is not None


# ------------------------------------------------------------------ settle / release / worst case


def test_settle_books_measured_usage_and_releases_the_rest(repo):
    t, run_id = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.complete_run(run_id, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_usage(usd=250_000, tin=100, tout=50))
    for bid in (f"task:{t.task_id}", f"root:{t.root_task_id}"):
        s = _sums(repo, bid)
        assert (s["reserve"], s["release"], s["settle"]) == (USD, -USD, 250_150)       # 250_100 in + 50 out tokens @ 1 micro/token
        assert repo.budget_status(bid).used_usd_micro == 250_150
    assert repo.budget_status(f"task:{t.task_id}").used_tokens == 250_150


def test_cancel_while_queued_releases_without_charging(repo):
    t = _approved(repo)
    q = repo.queue_task(t.task_id, t.version)
    repo.transition(q.task_id, Event.CANCEL, HUMAN, q.version)
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 0
    assert any(e.event_type == "budget.released" for e in repo.iter_events())


def test_cancel_running_without_usage_settles_at_worst_case(repo):
    t, _ = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.transition(t.task_id, Event.CANCEL, HUMAN, t.version)
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == USD


def test_abandoned_run_settles_at_worst_case_then_requeues_with_a_new_reservation(repo, clock):
    t, run_id = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(Limits().lease_ttl_ms)
    rep = repo.recover_leases()
    assert [a.action for a in rep.actions] == ["requeued"]
    ev = [e for e in repo.iter_events() if e.event_type == "budget.settled"][-1]
    assert ev.payload["worst_case"] is True and ev.run_id == run_id
    # attempt 1 charged the FULL reservation; attempt 2 holds a fresh one
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 2 * USD  # 1 worst-case settle + 1 open reserve
    assert budget_sql.open_task_reserve(repo._conn(), t.task_id, 2) is not None
    assert repo.get_task(t.task_id).state is TaskState.QUEUED


def test_overrun_is_recorded_truthfully_and_exhausts_the_budget(repo):
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
    rb = f"root:{root.task_id}"
    repo.set_budget(rb, "root", root.task_id, 1_200_000, 5_000_000, HUMAN)
    a = approve_fully(repo, repo.create_task(make_contract(), PLANNER, root.task_id).task)
    b = approve_fully(repo, repo.create_task(make_contract(), PLANNER, root.task_id).task)
    a = repo.queue_task(a.task_id, a.version)
    run = repo.start_run(a.task_id, "w1", EXECUTOR.id, a.version)
    repo.complete_run(run.run_id, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_usage(usd=1_500_000))  # > reserved
    st = repo.budget_status(rb)
    assert st.used_usd_micro == 1_500_000 and st.state == "exhausted"          # truth recorded, hard stop engaged
    blocked = repo.queue_task(b.task_id, b.version)
    assert blocked.state is TaskState.BLOCKED and blocked.blocked_reason is BlockReason.BUDGET_INSUFFICIENT
    assert any("is exhausted" in str(e.payload.get("reason")) for e in repo.iter_events() if e.event_type == "budget.reservation_refused")


# ------------------------------------------------------------------ limits at every level (boundary: limit ok, limit-1 refused)


def test_root_level_limit_boundary(repo):
    for limit, expect in ((USD, TaskState.QUEUED), (USD - 1, TaskState.BLOCKED)):
        root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
        repo.set_budget(f"root:{root.task_id}", "root", root.task_id, limit, 10_000_000, HUMAN)
        t = approve_fully(repo, repo.create_task(make_contract(), PLANNER, root.task_id).task)
        out = repo.queue_task(t.task_id, t.version)
        assert out.state is expect, limit
        if expect is TaskState.BLOCKED:  # nothing was reserved at ANY level (all-or-nothing)
            assert [e for e in repo.list_ledger() if e.task_id == t.task_id] == []


def test_global_daily_limit_and_new_day_rolls_over(repo, clock):
    gid = budget_sql.global_budget_id(clock.now_ms())
    repo.set_budget(gid, "global_daily", gid[7:], USD, 10_000_000, HUMAN)
    a, b = _approved(repo), _approved(repo)
    assert repo.queue_task(a.task_id, a.version).state is TaskState.QUEUED
    blocked = repo.queue_task(b.task_id, b.version)
    assert blocked.state is TaskState.BLOCKED
    clock.advance(24 * 3600 * 1000)  # next UTC day -> a fresh global budget id
    assert budget_sql.global_budget_id(clock.now_ms()) != gid
    resumed = repo.enqueue(blocked.task_id, Event.UNBLOCK, HUMAN, blocked.version)
    assert resumed.state is TaskState.QUEUED


def test_per_task_limit_exhaustion_blocks_the_retry(repo):
    t = repo.create_task(make_contract(resource_limits=ResourceLimits(max_usd_micro=USD, max_retries=1)), HUMAN).task
    t, run_id = to_running(repo, t)
    assert repo.budget_status(f"task:{t.task_id}").limit_usd_micro == 2 * USD  # cap x (1 + max_retries)
    out = repo.complete_run(run_id, "w1", RunState.FAILED, retryable=True, usage=_usage(usd=2 * USD))
    assert out.state is TaskState.BLOCKED and out.blocked_reason is BlockReason.BUDGET_INSUFFICIENT
    assert "per-task budget exhausted" in [e for e in repo.iter_events() if e.event_type == "budget.reservation_refused"][-1].payload["reason"]


def test_real_adapter_budget_defaults_to_zero_until_a_human_grants_it(open_repo):
    repo = open_repo(real_roles=["executor"])
    t = _approved(repo)
    out = repo.queue_task(t.task_id, t.version)
    assert out.state is TaskState.BLOCKED
    assert repo.budget_status("role:executor").limit_usd_micro == 0
    # mock roles are unaffected by the role level
    other = approve_fully(repo, repo.create_task(free_contract(assigned_role=Role.CHATGPT_WORK, approval_requirements=[
        {"type": "external_handoff", "reason": "x"}]), HUMAN).task)
    assert repo.queue_task(other.task_id, other.version).state is TaskState.QUEUED


# ------------------------------------------------------------------ increases: human approval bound to the contract hash


def _grant_flow(repo, task, budget_id, usd, tokens):
    ap = repo.request_budget_increase(task.task_id, budget_id, usd, tokens, PLANNER)
    repo.decide_approval(ap.approval_id, True, HUMAN, "ok")
    return ap


def test_increase_unblocks_real_adapter_end_to_end(open_repo):
    repo = open_repo(real_roles=["executor"])
    t = _approved(repo)
    blocked = repo.queue_task(t.task_id, t.version)
    ap = repo.request_budget_increase(t.task_id, "role:executor", USD, 200_000, PLANNER)
    assert (ap.approval_type, ap.task_version, ap.contract_sha256) == (ApprovalType.BUDGET_INCREASE, 1, t.contract_sha256)
    with pytest.raises(NotAuthorized):  # pending -> cannot be applied
        repo.grant_budget(ap.approval_id)
    repo.decide_approval(ap.approval_id, True, HUMAN, "approved")
    entry = repo.grant_budget(ap.approval_id)
    assert entry.kind.value == "grant" and entry.usd_micro == USD and entry.approval_id == ap.approval_id
    assert repo.grant_budget(ap.approval_id).entry_id == entry.entry_id                      # single use
    assert len([e for e in repo.list_ledger("role:executor") if e.kind.value == "grant"]) == 1
    assert repo.budget_status("role:executor").limit_usd_micro == USD
    resumed = repo.enqueue(blocked.task_id, Event.UNBLOCK, HUMAN, blocked.version)
    assert resumed.state is TaskState.QUEUED


def test_no_agent_or_system_can_decide_a_budget_approval(repo):
    t = _approved(repo)
    ap = repo.request_budget_increase(t.task_id, f"root:{t.root_task_id}", USD, 1000, DIRECTOR)
    for actor in (*AGENT_ACTORS, SYSTEM):
        with pytest.raises(NotAuthorized):
            repo.decide_approval(ap.approval_id, True, actor)


def test_executor_cannot_even_request_an_increase(repo):
    t = _approved(repo)
    with pytest.raises(NotAuthorized):
        repo.request_budget_increase(t.task_id, f"root:{t.root_task_id}", USD, 1, EXECUTOR)


def test_contract_change_invalidates_budget_approval_and_grant_is_refused(repo, db_path):
    t = _approved(repo)
    bid = f"root:{t.root_task_id}"
    ap = _grant_flow(repo, t, bid, USD, 1000)
    changed = make_contract(idempotency_key=t.idempotency_key, research_objective="different contract")
    repo.amend_task(t.task_id, changed, t.version, HUMAN)
    with pytest.raises(NotAuthorized):
        repo.grant_budget(ap.approval_id)          # service: approval was invalidated by the amendment
    # force the row back to 'granted' with a human decider: the binding is still stale -> service refuses ...
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("UPDATE approvals SET status='granted' WHERE approval_id=?", (ap.approval_id,))
    with pytest.raises(StaleApproval):
        repo.grant_budget(ap.approval_id)
    raw.execute("UPDATE approvals SET status='granted' WHERE approval_id=?", (ap.approval_id,))
    # ... and the DATABASE refuses a raw ledger insert that cites it
    with pytest.raises(sqlite3.DatabaseError, match="bound to the current contract hash"):
        raw.execute("INSERT INTO budget_ledger(entry_id,budget_id,task_id,kind,usd_micro,tokens,approval_id,idempotency_key,created_at_ms)"
                    " VALUES('x',?,?,'grant',?,?,?,'k-raw',1)", (bid, t.task_id, USD, 1000, ap.approval_id))
    raw.close()


def test_database_refuses_grant_with_inflated_amount_or_agent_decider(repo, db_path):
    t = _approved(repo)
    bid = f"root:{t.root_task_id}"
    ap = _grant_flow(repo, t, bid, USD, 1000)
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="exact amounts"):
        raw.execute("INSERT INTO budget_ledger(entry_id,budget_id,task_id,kind,usd_micro,tokens,approval_id,idempotency_key,created_at_ms)"
                    " VALUES('x',?,?,'grant',?,?,?,'k1',1)", (bid, t.task_id, USD * 100, 1000, ap.approval_id))
    with pytest.raises(sqlite3.DatabaseError, match="exact amounts"):
        raw.execute("INSERT INTO budget_ledger(entry_id,budget_id,task_id,kind,usd_micro,tokens,approval_id,idempotency_key,created_at_ms)"
                    " VALUES('x','root:other',?,'grant',?,?,?,'k2',1)", (t.task_id, USD, 1000, ap.approval_id))
    raw.close()


def test_base_limits_are_immutable(repo, db_path):
    t = _approved(repo)
    bid = f"root:{t.root_task_id}"
    repo.set_budget(bid, "root", t.root_task_id, USD, 1000, HUMAN)
    with pytest.raises(GuardFailed):
        repo.set_budget(bid, "root", t.root_task_id, 99 * USD, 1000, HUMAN)
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw.execute("UPDATE budgets SET limit_usd_micro=limit_usd_micro*100 WHERE budget_id=?", (bid,))
    raw.close()
    for actor in (*AGENT_ACTORS, SYSTEM):
        with pytest.raises(NotAuthorized):
            repo.set_budget("root:zzz", "root", "zzz", 1, 1, actor)


# ------------------------------------------------------------------ concurrency + invariant


def test_concurrent_queueing_never_over_commits_the_budget(repo):
    root = repo.create_task(free_contract(kind=TaskKind.OBJECTIVE, assigned_role=Role.DIRECTOR), HUMAN).task
    rb = f"root:{root.task_id}"
    repo.set_budget(rb, "root", root.task_id, USD, 100_000_000, HUMAN)  # room for exactly ONE reservation
    tasks = [approve_fully(repo, repo.create_task(make_contract(), PLANNER, root.task_id).task) for _ in range(6)]
    barrier = threading.Barrier(len(tasks))

    def go(t):
        barrier.wait()
        return repo.queue_task(t.task_id, t.version).state

    with ThreadPoolExecutor(len(tasks)) as pool:
        states = list(pool.map(go, tasks))
    assert states.count(TaskState.QUEUED) == 1 and states.count(TaskState.BLOCKED) == 5
    st = repo.budget_status(rb)
    assert st.used_usd_micro <= st.limit_usd_micro


def test_budget_ledger_invariant_holds_after_a_mixed_workload(repo, clock):
    for i in range(4):
        t, run_id = to_running(repo, repo.create_task(make_contract(), HUMAN).task, worker=f"w{i}")
        if i % 2:
            repo.complete_run(run_id, f"w{i}", RunState.SUCCEEDED, verifier_passed=True, usage=_usage(usd=10_000 * i))
        else:
            repo.complete_run(run_id, f"w{i}", RunState.FAILED, retryable=False, usage=_usage(usd=5))
    gid = budget_sql.global_budget_id(clock.now_ms())
    st = repo.budget_status(gid)
    assert 0 <= st.used_usd_micro <= st.limit_usd_micro
    # every reserve row is closed exactly once
    ledger = repo.list_ledger()
    reserves = {e.entry_id for e in ledger if e.kind.value == "reserve"}
    released = [e.reserve_ref for e in ledger if e.kind.value == "release"]
    assert sorted(released) == sorted(reserves) and len(set(released)) == len(released)
    assert ActorKind.SYSTEM  # silence unused import in some configurations
