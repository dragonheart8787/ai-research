"""A6 credit pool / reconciliation / quota probe, A7 billing integrity, A8 orphaned artifacts. Everything is fixture-driven:
no provider, no CLI, no network."""
from __future__ import annotations

import sqlite3

import pytest

from airos.agents.real.claude_executor import LoopResult
from airos.domain.limits import Limits
from airos.domain.schemas import Artifact, ArtifactClass, ArtifactRef, TrustLevel
from airos.domain.states import ActorKind, BlockReason, Event, RunState, TaskState
from airos.errors import GuardFailed, NotAuthorized
from airos.persistence import budget_sql
from airos.persistence.sqlite_repo import SYSTEM
from airos.quota_probe import QuotaReading, run_quota_probe
from tests.conftest import EXECUTOR, HUMAN, HUMAN2, PLANNER, approve_fully, make_contract, to_running
from tests.real_helpers import (
    ScriptedLoop,
    claude_fx,
    plan_json,
    pool_used,
    real_world,
    result_writer,
    role_contract,
)

USD = 1_000_000


def _pool(repo):
    return budget_sql.pool_budget_id(repo.clock.now_ms())


def _alloc(repo, role):
    return budget_sql.alloc_budget_id(role, repo.clock.now_ms())


# ------------------------------------------------------------------ A6: one pool, per-role allocations


def test_default_allocations_cap_and_unallocated_headroom():
    lim = Limits()
    assert lim.agent_sdk_credit_monthly_cap_usd_micro == 15 * USD
    assert lim.role_allocations_usd_micro == {"executor": 10 * USD, "planner": 3 * USD, "ai_scientist": 0}
    assert lim.agent_sdk_credit_monthly_cap_usd_micro - sum(lim.role_allocations_usd_micro.values()) == 2 * USD    # unallocated
    assert lim.director_max_runs_per_day == 0


def test_an_allocation_stops_a_role_before_the_pool_does(open_repo, tmp_path):
    repo = open_repo(limits=Limits(agent_sdk_credit_monthly_cap_usd_micro=3 * USD,
                                   role_allocations_usd_micro={"executor": USD, "planner": USD}))
    w = real_world(repo, tmp_path, loop=ScriptedLoop(LoopResult("d", files=["out/result.md"], tokens_in=1, tokens_out=1, auth_source="oauth"),
                                                     writer=result_writer), roles=("executor",))
    first = approve_fully(repo, repo.create_task(role_contract("executor"), HUMAN).task)
    w.worker.tick()
    assert repo.get_task(first.task_id).state is TaskState.REVIEW
    second = approve_fully(repo, repo.create_task(role_contract("executor"), HUMAN).task)     # reserves 1 USD but only ~1 USD - 2 micro left
    w.worker.tick()
    cur = repo.get_task(second.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.CREDIT_EXHAUSTED
    assert pool_used(repo, "executor") == 2 and pool_used(repo) == 2                           # only the real (tiny) spend was booked


def test_the_pool_stops_roles_even_when_their_own_allocation_has_room(open_repo, tmp_path):
    repo = open_repo(limits=Limits(agent_sdk_credit_monthly_cap_usd_micro=USD,
                                   role_allocations_usd_micro={"executor": 5 * USD, "planner": 5 * USD}))
    w = real_world(repo, tmp_path, fixtures=[claude_fx(plan_json(1), usage=(1, 1))], roles=("planner", "executor"),
                   loop=ScriptedLoop(LoopResult("d", files=["out/result.md"], tokens_in=1, tokens_out=1, auth_source="oauth"), writer=result_writer))
    a = approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    w.worker.tick()
    assert repo.get_task(a.task_id).state is TaskState.REVIEW
    b = approve_fully(repo, repo.create_task(role_contract("executor"), HUMAN).task)
    w.worker.tick()
    assert repo.get_task(b.task_id).blocked_reason is BlockReason.CREDIT_EXHAUSTED            # the shared pool, not the allocation, is short


def test_raising_an_allocation_needs_a_human_approval_bound_to_the_contract_hash(open_repo):
    repo = open_repo(real_roles=["executor"], limits=Limits(role_allocations_usd_micro={"executor": 0}))
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    bid = _alloc(repo, "executor")
    ap = repo.request_budget_increase(t.task_id, bid, 2 * USD, 10**9, PLANNER)
    assert ap.contract_sha256 == t.contract_sha256 and ap.task_version == t.contract_version
    with pytest.raises(NotAuthorized):
        repo.grant_budget(ap.approval_id)                                                       # undecided
    for agent in (PLANNER, EXECUTOR, SYSTEM):
        with pytest.raises(NotAuthorized):
            repo.decide_approval(ap.approval_id, True, agent)                                    # only a human decides
    repo.decide_approval(ap.approval_id, True, HUMAN, "ok")
    repo.amend_task(t.task_id, make_contract(idempotency_key=t.idempotency_key, research_objective="changed"), t.version, HUMAN)
    with pytest.raises(NotAuthorized):
        repo.grant_budget(ap.approval_id)                                                       # contract changed since: the grant is refused
    assert not [e for e in repo.list_ledger(bid) if e.kind.value == "grant"]


def test_the_director_run_cap_counts_runs_per_utc_day_and_is_human_set(open_repo, tmp_path, clock):
    repo = open_repo(limits=Limits(director_max_runs_per_day=1))
    from tests.real_helpers import codex_exec_fx, codex_status_fx

    w = real_world(repo, tmp_path, fixtures=[codex_status_fx(), codex_exec_fx(usage=(1, 1)), codex_status_fx(), codex_exec_fx(usage=(1, 1))],
                   roles=("director",))
    a = approve_fully(repo, repo.create_task(role_contract("director"), HUMAN).task)
    w.worker.tick()
    assert repo.get_task(a.task_id).state is TaskState.REVIEW
    b = approve_fully(repo, repo.create_task(role_contract("director"), HUMAN).task)
    w.worker.tick()
    assert repo.get_task(b.task_id).state is TaskState.BLOCKED and repo.get_task(b.task_id).blocked_reason is BlockReason.BUDGET_INSUFFICIENT
    clock.advance(24 * 3600 * 1000)                                                             # a new UTC day
    cur = repo.get_task(b.task_id)
    repo.enqueue(cur.task_id, Event.UNBLOCK, HUMAN, cur.version)
    w.worker.tick()
    assert repo.get_task(b.task_id).state is TaskState.REVIEW


# ------------------------------------------------------------------ A6: credit exhaustion


def _exhausted_world(repo, tmp_path, n=1):
    msg = "Claude usage limit reached for this plan"
    return real_world(repo, tmp_path, fixtures=[claude_fx(msg, returncode=1, is_error=True, usage=None) for _ in range(n)]
                      + [claude_fx(plan_json(1), usage=(1, 1))], roles=("planner",))


def test_credit_exhaustion_blocks_for_a_human_is_audited_and_never_auto_retried(real_repo, tmp_path, clock):
    repo = real_repo
    w = _exhausted_world(repo, tmp_path)
    t = approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.CREDIT_EXHAUSTED
    assert cur.attempt_count - cur.uncounted_attempts == 0                                      # does not count toward retries
    assert any(e.event_type == "adapter.response_rejected" and e.payload["category"] == "credit_exhausted" for e in repo.iter_events())
    n_calls = len(w.runner.calls)
    clock.advance(3600 * 1000)
    w.worker.tick()
    w.worker.tick()
    assert len(w.runner.calls) == n_calls                                                        # no automatic retry, ever
    cur = repo.get_task(t.task_id)
    with pytest.raises(GuardFailed):                                                             # the system cannot clear it
        repo.enqueue(cur.task_id, Event.UNBLOCK, SYSTEM, cur.version, __import__("airos.domain.guards", fromlist=["GuardContext"]).GuardContext(blocker_cleared=True))
    assert repo.verify_chain().ok


def test_repeated_exhaustion_never_uses_up_the_retry_limit(real_repo, tmp_path):
    repo = real_repo
    w = _exhausted_world(repo, tmp_path, n=4)
    t = approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    for _ in range(4):                                                                           # more than max_retries (2) + 1
        w.worker.tick()
        cur = repo.get_task(t.task_id)
        assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.CREDIT_EXHAUSTED
        repo.enqueue(cur.task_id, Event.UNBLOCK, HUMAN, cur.version)                              # the human resumes each time
    w.runner.fixtures[:] = [claude_fx(plan_json(1), usage=(1, 1))]                                # the credit is back (fixtures match first-wins)
    w.worker.tick()
    assert repo.get_task(t.task_id).state is TaskState.REVIEW                                     # finally succeeds: retries were never consumed


# ------------------------------------------------------------------ A6: reconciliation


def _book_spend(repo, tmp_path):
    w = real_world(repo, tmp_path, fixtures=[claude_fx(plan_json(1), usage=(1000, 1000))], roles=("planner",))
    approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    w.worker.tick()
    assert pool_used(repo) == 2000


def test_reconciliation_is_human_only_and_records_the_delta(real_repo, tmp_path):
    repo = real_repo
    _book_spend(repo, tmp_path)
    pool = _pool(repo)
    for actor in (SYSTEM, PLANNER, EXECUTOR):
        with pytest.raises(NotAuthorized):
            repo.record_reconciliation(pool, 15 * USD - 2000, actor)
    res = repo.record_reconciliation(pool, 15 * USD - 2000, HUMAN, "matches the account page")
    assert (res.delta_usd_micro, res.flagged, res.frozen) == (0, False, False)
    assert any(e.kind.value == "reconciliation" and e.budget_id == pool for e in repo.list_ledger(pool))


def test_drift_above_fifteen_percent_is_flagged_and_an_underestimate_freezes_until_a_human_grant(real_repo, tmp_path):
    repo = real_repo
    _book_spend(repo, tmp_path)
    pool = _pool(repo)
    under = repo.record_reconciliation(pool, 15 * USD - 2000 - 3 * USD, HUMAN)                   # reality has LESS left: we under-counted
    assert under.flagged and under.frozen and under.delta_usd_micro == 3 * USD
    assert any(e.event_type == "budget.drift_flagged" and e.payload["estimate_ran_lower_than_reality"] for e in repo.iter_events())
    t = approve_fully(repo, repo.create_task(role_contract("planner"), HUMAN).task)
    out = repo.queue_task(t.task_id, t.version)
    assert out.state is TaskState.BLOCKED and out.blocked_reason is BlockReason.CREDIT_EXHAUSTED  # frozen: nothing new reserves
    ap = repo.request_budget_increase(t.task_id, _alloc(repo, "planner"), USD, 10**9, PLANNER)
    repo.decide_approval(ap.approval_id, True, HUMAN, "reviewed the drift")
    repo.grant_budget(ap.approval_id)                                                            # the human-approved grant unfreezes
    again = repo.get_task(t.task_id)
    assert repo.enqueue(again.task_id, Event.UNBLOCK, HUMAN, again.version).state is TaskState.QUEUED


def test_an_overestimate_is_flagged_but_does_not_freeze(real_repo, tmp_path):
    repo = real_repo
    _book_spend(repo, tmp_path)
    over = repo.record_reconciliation(_pool(repo), 15 * USD - 2000 + 3 * USD, HUMAN)             # reality has MORE left: our estimate ran high
    assert over.flagged and not over.frozen and over.delta_usd_micro == -3 * USD


def test_small_drift_is_not_flagged(real_repo, tmp_path):
    repo = real_repo
    _book_spend(repo, tmp_path)
    assert not repo.record_reconciliation(_pool(repo), 15 * USD - 2000 - USD, HUMAN).flagged     # ~6.7 % < 15 %


# ------------------------------------------------------------------ A6: the optional quota probe


class _Probe:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def read(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("endpoint down")
        return QuotaReading(remaining_usd_micro=7 * USD, source="synthetic test probe")


def test_the_probe_is_disabled_by_default_and_a_disabled_probe_is_never_called(repo):
    p = _Probe()
    assert run_quota_probe(repo, p) is None and run_quota_probe(repo, p, enabled=False) is None and p.calls == 0
    assert run_quota_probe(repo, None, enabled=True) is None
    assert not [e for e in repo.iter_events() if e.event_type == "quota.probe"]


def test_a_failing_probe_is_harmless_and_a_working_one_changes_no_budget(real_repo, tmp_path):
    repo = real_repo
    _book_spend(repo, tmp_path)
    before = (pool_used(repo), repo.budget_status(_pool(repo)).limit_usd_micro, [t.state for t in repo.list_tasks()])
    assert run_quota_probe(repo, _Probe(fail=True), enabled=True) is None                        # never raises
    reading = run_quota_probe(repo, _Probe(), enabled=True)
    assert reading is not None and reading.remaining_usd_micro == 7 * USD
    ev = [e for e in repo.iter_events() if e.event_type == "quota.probe"]
    assert [e.payload["ok"] for e in ev] == [False, True] and ev[1].payload["informational_only"] is True
    assert before == (pool_used(repo), repo.budget_status(_pool(repo)).limit_usd_micro, [t.state for t in repo.list_tasks()])   # not a budget input


def test_no_budget_or_routing_code_reads_the_probe():
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "airos"
    users = [str(p.relative_to(src)) for p in src.rglob("*.py") if "quota_probe" in p.read_text() and p.name not in ("quota_probe.py", "config.py")]
    assert users == []


# ------------------------------------------------------------------ A7: billing integrity


@pytest.mark.parametrize("source,held", [("oauth", False), ("ANTHROPIC_API_KEY", True), ("user", True), (None, True)])
def test_a_reported_auth_path_other_than_subscription_oauth_holds_the_task_for_a_human(real_repo, tmp_path, source, held):
    repo = real_repo
    loop = ScriptedLoop(LoopResult("d", files=["out/result.md"], tokens_in=1, tokens_out=1, auth_source=source), writer=result_writer)
    w = real_world(repo, tmp_path, loop=loop, roles=("executor",))
    t = approve_fully(repo, repo.create_task(role_contract("executor"), HUMAN).task)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.REVIEW and cur.review_hold is held
    audited = [e for e in repo.iter_events() if e.event_type == "billing.integrity_mismatch"]
    assert bool(audited) is held
    if held:
        assert audited[0].payload["expected_auth_path"] == "subscription_oauth"
        for actor in (SYSTEM, EXECUTOR, PLANNER):
            with pytest.raises((NotAuthorized, GuardFailed)):
                repo.transition(cur.task_id, Event.ACCEPT, actor, cur.version)
        assert repo.transition(cur.task_id, Event.ACCEPT, HUMAN, cur.version).state is TaskState.COMPLETED     # only a human can review it
    assert repo.verify_chain().ok


def test_only_the_system_can_place_a_hold_and_the_hold_kind_is_fixed(repo):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    for actor in (HUMAN, PLANNER, EXECUTOR):
        with pytest.raises(NotAuthorized):
            repo.hold_for_human_review(t.task_id, "billing.integrity_mismatch", {}, actor=actor)
    with pytest.raises(ValueError):
        repo.hold_for_human_review(t.task_id, "something.else", {})


# ------------------------------------------------------------------ A8: orphaned artifacts


def _artifact(repo, run_id, n, cls=ArtifactClass.WORK_PRODUCT):
    return Artifact(artifact_id=f"orph-{n}", sha256="b" * 64, size_bytes=1, media_type="text/plain", artifact_class=cls, logical_name="o.txt",
                    storage_uri="x", producer_run_id=run_id, producer_principal_id=None, trust_level=TrustLevel.UNTRUSTED,
                    created_at_ms=repo.clock.now_ms())


@pytest.mark.parametrize("end", [RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED])
def test_artifacts_of_a_run_that_did_not_succeed_are_orphaned_and_audited(repo, clock, end):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.add_artifact(_artifact(repo, rid, f"a-{end.value}"), SYSTEM)
    assert not repo.get_artifact(f"orph-a-{end.value}").orphaned
    if end is RunState.TIMED_OUT:
        clock.advance(24 * 3600 * 1000)
        repo.time_out_run(rid)
    elif end is RunState.FAILED:
        repo.complete_run(rid, "w1", RunState.FAILED, retryable=False)
    else:
        repo.transition(t.task_id, Event.CANCEL, HUMAN, repo.get_task(t.task_id).version)
    art = repo.get_artifact(f"orph-a-{end.value}")
    assert art.orphaned and art.orphaned_reason
    assert any(e.event_type == "artifact.orphaned" and f"orph-a-{end.value}" in e.payload["artifact_ids"] for e in repo.iter_events())


def test_a_successful_run_keeps_its_artifacts(repo):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.add_artifact(_artifact(repo, rid, "ok"), SYSTEM)
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True)
    assert not repo.get_artifact("orph-ok").orphaned


def test_timing_race_an_artifact_registered_after_the_deadline_is_born_orphaned(repo, clock):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    clock.advance(24 * 3600 * 1000)
    repo.time_out_run(rid)                                                                       # the run is over ...
    repo.add_artifact(_artifact(repo, rid, "late"), SYSTEM)                                       # ... its result arrives afterwards
    late = repo.get_artifact("orph-late")
    assert late.orphaned and "after its run ended" in (late.orphaned_reason or "")
    assert any(e.event_type == "artifact.orphaned" and e.payload["artifact_ids"] == ["orph-late"] for e in repo.iter_events())


def test_an_orphan_can_never_be_an_input_be_read_by_an_agent_or_be_un_orphaned(repo, db_path):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.add_artifact(_artifact(repo, rid, "x"), SYSTEM)
    repo.complete_run(rid, "w1", RunState.FAILED, retryable=False)
    ref = ArtifactRef(artifact_id="orph-x", sha256="b" * 64)
    with pytest.raises(GuardFailed, match="orphaned"):
        repo.create_task(make_contract(input_artifact_refs=[ref]), HUMAN)
    for actor in (EXECUTOR, PLANNER):
        with pytest.raises(NotAuthorized, match="orphaned"):
            repo.authorize_artifact_read(actor, "orph-x", None)
    repo.authorize_artifact_read(HUMAN, "orph-x", None)                                           # a human may still inspect it
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="un-orphaned"):
            conn.execute("UPDATE artifacts SET orphaned=0 WHERE artifact_id='orph-x'")
        with pytest.raises(sqlite3.DatabaseError, match="only an orphaned"):                      # removal of a non-human / bogus remover
            conn.execute("UPDATE artifacts SET removed_at_ms=1, removed_by='executor-1' WHERE artifact_id='orph-x'")
    finally:
        conn.close()


def test_removal_is_human_only_and_only_for_orphans(repo):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.add_artifact(_artifact(repo, rid, "keep"), SYSTEM)
    for actor in (SYSTEM, EXECUTOR, PLANNER):
        with pytest.raises(NotAuthorized):
            repo.remove_orphaned_artifact("orph-keep", actor)
    with pytest.raises(GuardFailed, match="only an orphaned"):
        repo.remove_orphaned_artifact("orph-keep", HUMAN)                                         # not orphaned: refused even for a human
    repo.complete_run(rid, "w1", RunState.FAILED, retryable=False)
    removed = repo.remove_orphaned_artifact("orph-keep", HUMAN2)
    assert removed.removed_at_ms is not None
    assert any(e.event_type == "artifact.removed" for e in repo.iter_events())
    assert ActorKind.HUMAN
