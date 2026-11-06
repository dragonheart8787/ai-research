"""Authorization: the role-by-action matrix is data; every (role, action) pair is enumerated against it, and
denials are audited through a separate connection with deduplication and a flood limit."""
from __future__ import annotations

import itertools
import json

import pytest

from airos.domain import machine
from airos.domain.guards import GuardContext
from airos.domain.schemas import ApprovalType, Artifact, ArtifactClass, TrustLevel
from airos.domain.states import AGENT_KINDS, Actor, ActorKind, Event
from airos.errors import ActorNotAllowed, GuardFailed, NotAuthorized
from airos.persistence import denials
from airos.persistence.sqlite_repo import SYSTEM
from airos.security import permissions
from tests.conftest import (
    AGENT_ACTORS,
    CGW,
    DIRECTOR,
    EXECUTOR,
    HUMAN,
    PLANNER,
    SCI,
    approve_fully,
    make_contract,
)

ACTOR_OF_KIND = {
    ActorKind.HUMAN: HUMAN, ActorKind.SYSTEM: SYSTEM, ActorKind.DIRECTOR: DIRECTOR, ActorKind.PLANNER: PLANNER,
    ActorKind.EXECUTOR: EXECUTOR, ActorKind.CHATGPT_WORK: CGW, ActorKind.AI_SCIENTIST: SCI,
}
K = ActorKind
# Independent oracle for repository operations (written out by hand, NOT read from permissions.json).
EXPECTED_OPS: dict[str, set[ActorKind]] = {
    "task.create_root": {K.HUMAN},
    "task.create_root_as_director": {K.DIRECTOR},
    "task.create_child": {K.HUMAN, K.PLANNER},
    "task.add_dependency": {K.HUMAN, K.PLANNER},
    "task.amend": {K.HUMAN, K.PLANNER},
    "approval.request": {K.HUMAN, K.SYSTEM, K.DIRECTOR, K.PLANNER},
    "approval.decide": {K.HUMAN},
    "budget.request_increase": {K.HUMAN, K.SYSTEM, K.DIRECTOR, K.PLANNER},
    "budget.set": {K.HUMAN},
    "budget.reserve": {K.SYSTEM},
    "budget.grant": {K.HUMAN, K.SYSTEM},
    "run.start": {K.SYSTEM}, "run.heartbeat": {K.SYSTEM}, "run.complete": {K.SYSTEM}, "run.recover": {K.SYSTEM},
    "artifact.register": {K.HUMAN, K.SYSTEM},
    "artifact.release": {K.HUMAN},
    "audit.append_system_event": {K.SYSTEM},
    "cascade.cancel": {K.SYSTEM},
}
EVENT_ACTIONS = {f"event:{e.value}" for e in Event}


# ------------------------------------------------------------------ the matrix as data


def test_matrix_is_a_data_file_with_exactly_the_expected_actions():
    from importlib import resources

    raw = json.loads(resources.files("airos.security").joinpath("permissions.json").read_text())
    assert set(raw) == {"version", "actions", "artifact_read"}
    assert set(raw["actions"]) == set(EXPECTED_OPS) | EVENT_ACTIONS


def test_every_role_action_pair_matches_the_matrix():
    """Enumerates ALL (actor kind, action) pairs, including unknown actions (which must be denied)."""
    pairs = 0
    for kind, action in itertools.product(ActorKind, [*EXPECTED_OPS, *EVENT_ACTIONS, "nope", "task.delete", ""]):
        if action in EXPECTED_OPS:
            want = kind in EXPECTED_OPS[action]
        elif action in EVENT_ACTIONS:
            want = kind in {a for t in machine.TRANSITIONS if f"event:{t.event.value}" == action for a in t.actors}
        else:
            want = False
        assert permissions.is_allowed(kind, action) is want, (kind, action)
        if want:
            permissions.authorize(kind, action)
        else:
            with pytest.raises(NotAuthorized):
                permissions.authorize(kind, action)
        pairs += 1
    assert pairs == len(ActorKind) * (len(EXPECTED_OPS) + len(EVENT_ACTIONS) + 3)


def test_no_agent_role_can_approve_or_cancel_anywhere():
    for ev in (Event.APPROVE, Event.DENY, Event.REQUEST_CHANGES, Event.CANCEL):
        assert permissions.ACTIONS[f"event:{ev.value}"] == {K.HUMAN}
    for action in ("approval.decide", "budget.set", "artifact.release"):
        assert not (permissions.ACTIONS[action] & AGENT_KINDS) and K.SYSTEM not in permissions.ACTIONS[action]
    for t in machine.TRANSITIONS:
        if t.event in (Event.APPROVE, Event.DENY, Event.CANCEL, Event.REQUEST_CHANGES):
            assert not (t.actors & AGENT_KINDS) and K.SYSTEM not in t.actors


def test_artifact_read_matrix_every_kind_class_flag_combination():
    """Hand-written oracle for artifact reads. quarantined => only human/system (and the ai_scientist owner)."""
    always = {
        "request": {K.HUMAN, K.SYSTEM, K.DIRECTOR, K.PLANNER},
        "plan": {K.HUMAN, K.SYSTEM, K.DIRECTOR, K.PLANNER},
        "work_product": {K.HUMAN, K.SYSTEM, K.DIRECTOR},
        "external_handoff": {K.HUMAN, K.SYSTEM, K.DIRECTOR},
        "scientist_output": {K.HUMAN, K.SYSTEM, K.DIRECTOR},
        "eval_result": {K.HUMAN, K.SYSTEM, K.DIRECTOR, K.PLANNER},
        "final_evaluation": {K.HUMAN, K.SYSTEM},
    }
    inputs = {
        "request": {K.EXECUTOR, K.CHATGPT_WORK, K.AI_SCIENTIST}, "plan": {K.EXECUTOR, K.CHATGPT_WORK},
        "work_product": {K.PLANNER, K.EXECUTOR, K.CHATGPT_WORK}, "external_handoff": {K.PLANNER, K.EXECUTOR, K.CHATGPT_WORK},
        "scientist_output": {K.AI_SCIENTIST}, "eval_result": set(), "final_evaluation": set(),
    }
    n = 0
    for kind, cls, pinned, quarantined in itertools.product(ActorKind, always, (False, True), (False, True)):
        want = kind in always[cls] or (pinned and kind in inputs[cls])
        if quarantined and kind not in (K.HUMAN, K.SYSTEM, K.AI_SCIENTIST):
            want = False
        assert permissions.may_read_artifact(kind, cls, pinned_or_own=pinned, quarantined=quarantined) is want, \
            (kind, cls, pinned, quarantined)
        n += 1
    assert n == 7 * 7 * 4
    assert not permissions.may_read_artifact(K.HUMAN, "unknown_class", pinned_or_own=True, quarantined=False)


# ------------------------------------------------------------------ enforcement, enumerated per operation


def _ops(repo):
    """One callable per operation: op(actor). Business-rule failures (GuardFailed etc.) are fine; only
    NotAuthorized is the matrix speaking."""
    root = repo.create_task(make_contract(), HUMAN).task
    draft = repo.create_task(make_contract(), HUMAN).task
    other = repo.create_task(make_contract(), HUMAN).task
    planned = repo.transition(repo.create_task(make_contract(), HUMAN).task.task_id, Event.PLAN, PLANNER, 1, GuardContext(plan_ok=True))
    awaiting = repo.transition(planned.task_id, Event.REQUIRE_APPROVAL, SYSTEM, planned.version)
    pending = repo.request_approval(awaiting.task_id, ApprovalType.COST, SYSTEM)
    bi = repo.request_budget_increase(root.task_id, f"root:{root.root_task_id}", 5, 5, SYSTEM)
    repo.decide_approval(bi.approval_id, True, HUMAN)
    approved = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    cancelled = repo.transition(repo.create_task(make_contract(), HUMAN).task.task_id, Event.CANCEL, HUMAN, 1)
    counter = itertools.count()
    amend = make_contract(idempotency_key=draft.idempotency_key, research_objective="amended")
    art = lambda actor: Artifact(  # noqa: E731
        artifact_id=f"a{next(counter)}", sha256="a" * 64, size_bytes=1, media_type="t/p", artifact_class=ArtifactClass.REQUEST,
        logical_name="n", storage_uri="x", producer_principal_id=actor.id, trust_level=TrustLevel.TRUSTED, created_at_ms=1)
    return {
        "task.create_root": lambda a: repo.create_task(make_contract(), a),
        "task.create_root_as_director": lambda a: repo.create_task(make_contract(), a),  # flag off -> matrix row not consulted
        "task.create_child": lambda a: repo.create_task(make_contract(), a, root.task_id),
        "task.add_dependency": lambda a: repo.add_dependency(draft.task_id, other.task_id, a),
        "task.amend": lambda a: repo.amend_task(draft.task_id, amend, repo.get_task(draft.task_id).version, a),
        "approval.request": lambda a: repo.request_approval(awaiting.task_id, ApprovalType.EXECUTE, a),
        "approval.decide": lambda a: repo.decide_approval(pending.approval_id, True, a),
        "budget.request_increase": lambda a: repo.request_budget_increase(root.task_id, f"root:{next(counter)}", 1, 1, a),
        "budget.set": lambda a: repo.set_budget(f"root:set{next(counter)}", "root", "x", 1, 1, a),
        "budget.reserve": lambda a: repo.reserve_budget(approved.task_id, repo.get_task(approved.task_id).version, actor=a),
        "budget.grant": lambda a: repo.grant_budget(bi.approval_id, actor=a),
        "run.start": lambda a: repo.start_run(approved.task_id, "w", "executor-1", repo.get_task(approved.task_id).version, actor=a),
        "run.heartbeat": lambda a: repo.heartbeat("no-such-run", "w", actor=a),
        "run.complete": lambda a: repo.complete_run("no-such-run", "w", __import__("airos.domain.states", fromlist=["RunState"]).RunState.FAILED, actor=a),
        "run.recover": lambda a: repo.recover_leases(actor=a),
        "artifact.register": lambda a: repo.add_artifact(art(a), a),
        "artifact.release": lambda a: repo.release_artifact("no-such-artifact", a),
        "audit.append_system_event": lambda a: repo.record_system_event("worker.error", {"x": 1}, actor=a),
        "cascade.cancel": lambda a: repo.cascade_cancel_descendants(cancelled.task_id, actor=a),
    }


@pytest.mark.parametrize("action", sorted(EXPECTED_OPS))
def test_enforcement_matches_matrix_for_every_role(repo, action):
    ops = _ops(repo)
    for kind in ActorKind:
        if action == "task.create_root_as_director":
            continue  # only consulted when director_may_create_root is on (covered below)
        actor = ACTOR_OF_KIND[kind]
        allowed = kind in EXPECTED_OPS[action]
        try:
            ops[action](actor)
            denied = False
        except NotAuthorized:
            denied = True
        except Exception:  # noqa: BLE001 - business rule failures are not authorization
            denied = False
        assert denied is (not allowed), f"{action} as {kind.value}: denied={denied}, matrix allows={allowed}"


def test_director_may_create_root_only_when_the_flag_is_on(open_repo):
    from airos.domain.limits import Limits

    off, on = open_repo(), open_repo(limits=Limits(director_may_create_root=True))
    with pytest.raises(NotAuthorized):
        off.create_task(make_contract(), DIRECTOR)
    assert on.create_task(make_contract(), DIRECTOR).created
    with pytest.raises(NotAuthorized):
        on.create_task(make_contract(), EXECUTOR)


@pytest.mark.parametrize("actor", [*AGENT_ACTORS, SYSTEM], ids=lambda a: a.kind.value)
def test_no_agent_or_system_can_approve_deny_or_cancel(repo, actor):
    t = repo.create_task(make_contract(), HUMAN).task
    with pytest.raises(NotAuthorized):
        repo.transition(t.task_id, Event.CANCEL, actor, t.version)
    awaiting = repo.transition(repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True)).task_id,
                               Event.REQUIRE_APPROVAL, SYSTEM, 2)
    for ev in (Event.APPROVE, Event.DENY, Event.REQUEST_CHANGES, Event.CANCEL):
        with pytest.raises(ActorNotAllowed):
            repo.transition(awaiting.task_id, ev, actor, awaiting.version)
    ap = repo.request_approval(awaiting.task_id, ApprovalType.COST, SYSTEM)
    with pytest.raises(NotAuthorized):
        repo.decide_approval(ap.approval_id, True, actor)
    assert repo.get_task(t.task_id).state.value == "awaiting_approval"


def test_unregistered_principal_is_denied(repo):
    with pytest.raises(NotAuthorized):
        repo.create_task(make_contract(), Actor(ActorKind.HUMAN, "ghost"))


# ------------------------------------------------------------------ denial audit: separate connection, dedup, rate limit


def _denied_events(repo):
    return [e for e in repo.iter_events() if e.event_type == "authz.denied"]


def test_denial_is_audited_through_a_separate_connection_after_the_main_tx_rolled_back(repo, monkeypatch):
    t = repo.create_task(make_contract(), HUMAN).task
    seen = {}
    real = repo._denials.record

    def spy(actor, action, task_id, detail):
        seen["main_conn_in_tx"] = repo._conn().in_transaction
        seen["separate_conn"] = repo._denial_conn() is not repo._conn()
        return real(actor, action, task_id, detail)

    monkeypatch.setattr(repo._denials, "record", spy)
    head_before = repo.head().seq
    with pytest.raises(ActorNotAllowed):
        repo.transition(t.task_id, Event.CANCEL, EXECUTOR, t.version)
    assert seen == {"main_conn_in_tx": False, "separate_conn": True}
    [ev] = _denied_events(repo)
    assert (ev.actor_id, ev.actor_kind, ev.task_id) == ("executor-1", "executor", t.task_id)
    assert ev.payload["action"] == "event:cancel" and ev.seq == head_before + 1
    assert repo.get_task(t.task_id).version == t.version      # the denied operation changed nothing
    assert repo.verify_chain().ok                               # and the chain stays valid


def test_repeated_identical_denials_are_deduplicated_within_a_window(repo):
    t = repo.create_task(make_contract(), HUMAN).task
    for _ in range(400):
        with pytest.raises(ActorNotAllowed):
            repo.transition(t.task_id, Event.CANCEL, EXECUTOR, t.version)
    assert len(_denied_events(repo)) == 1
    st = repo._conn().execute("SELECT count, total FROM authz_denial_state WHERE key='executor-1|event:cancel'").fetchone()
    assert (st["count"], st["total"]) == (400, 400)


def test_denial_flood_cannot_grow_the_audit_log_without_bound(repo, clock):
    t = repo.create_task(make_contract(), HUMAN).task
    base = len(list(repo.iter_events()))
    actions = [Event.CANCEL, Event.APPROVE, Event.DENY, Event.REQUEST_CHANGES, Event.PLAN, Event.ACCEPT]
    # flood: many registered agents x many events x many task ids x many invented identities
    for i in range(500):
        actor = AGENT_ACTORS[i % len(AGENT_ACTORS)] if i % 2 else Actor(ActorKind.HUMAN, f"invented-{i}")
        with pytest.raises(NotAuthorized):
            repo.transition(t.task_id, actions[i % len(actions)], actor, t.version) if actor.id.startswith("invented") \
                else repo.create_task(make_contract(), actor)
    first_window = len(_denied_events(repo))
    assert first_window <= denials.MAX_EVENTS_PER_WINDOW
    # bounded state: one row per (principal bucket x action), never per attempt or per invented identity
    nstate = repo._conn().execute("SELECT COUNT(*) FROM authz_denial_state").fetchone()[0]
    assert nstate <= (len(AGENT_ACTORS) + 1) * (len(actions) + 2)
    assert "invented-1" not in " ".join(r[0] for r in repo._conn().execute("SELECT key FROM authz_denial_state"))
    # next window: the same flood adds a bounded number of events again, and reports what was suppressed
    clock.advance(denials.WINDOW_MS)
    for _ in range(300):
        with pytest.raises(NotAuthorized):
            repo.create_task(make_contract(), EXECUTOR)
    second = [e for e in _denied_events(repo)][first_window:]
    assert 1 <= len(second) <= denials.MAX_EVENTS_PER_WINDOW
    assert any(e.payload["suppressed_in_previous_window"] > 0 for e in second)
    total_new = len(list(repo.iter_events())) - base
    assert total_new <= 2 * denials.MAX_EVENTS_PER_WINDOW + 5     # 800 attempts -> bounded by windows, not attempts
    assert repo.verify_chain().ok


def test_global_window_cap_suppresses_even_distinct_keys(repo):
    # many distinct (actor, action) keys inside one window: capped at MAX_EVENTS_PER_WINDOW
    pairs = [(a, e) for a in AGENT_ACTORS for e in Event]
    t = repo.create_task(make_contract(), HUMAN).task
    n = 0
    for actor, ev in pairs * 2:
        try:
            repo.transition(t.task_id, ev, actor, t.version)
        except NotAuthorized:
            n += 1
        except GuardFailed:
            pass
        except Exception:  # noqa: BLE001
            pass
    assert n > 0 and len(_denied_events(repo)) <= denials.MAX_EVENTS_PER_WINDOW
