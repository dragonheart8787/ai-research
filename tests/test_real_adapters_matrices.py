"""Real adapters are held to the SAME matrices as mocks: routing, budget, and permission outcomes.
(Real = fixture-backed; nothing here touches a network.)"""
from __future__ import annotations

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.real.claude_executor import LoopResult
from airos.agents.real.factory import SCIENTIST_STUB_REASON, real_factories
from airos.agents.real.handoff import HandoffConfig
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.domain.guards import GuardContext
from airos.domain.schemas import ArtifactClass, Role, TrustLevel
from airos.domain.states import BlockReason, Event, TaskState
from airos.errors import ActorNotAllowed, AdapterUnavailable, GuardFailed
from airos.persistence import budget_sql
from tests.conftest import DIRECTOR, EXECUTOR, HUMAN, TEST_PRICES, approve_fully
from tests.real_helpers import (
    ENV,
    ScriptedLoop,
    anthropic_fx,
    external_workflow,
    git,
    grant_role_budget,
    make_bare_repo,
    openai_fx,
    real_world,
    result_writer,
    role_contract,
)

REAL_ROLES = ["director", "planner", "executor", "chatgpt_work"]
CRED = {"director": "TEST_OPENAI_KEY", "planner": "TEST_ANTHROPIC_PLANNER_KEY", "executor": "TEST_ANTHROPIC_EXEC_KEY",
        "chatgpt_work": None, "ai_scientist": "TEST_OPENAI_KEY"}
MODEL = {"director": "gpt-test", "planner": "claude-test", "executor": "claude-test", "chatgpt_work": None, "ai_scientist": "gpt-test"}
TOKENS = {"director": (120, 30), "planner": (20, 8), "executor": (50, 10), "chatgpt_work": (0, 0)}


def _factories(tmp_path, bare):
    class NoNet:
        def send(self, request):
            raise AssertionError("fixture-only test reached a transport")

    return real_factories(transport=NoNet(), executor_loop=ScriptedLoop(),
                          handoff=HandoffConfig(repo_path=bare, allowed_authors=frozenset({"a@example.invalid"}), work_root=tmp_path / "cp"))


def _registry(role, scenario, tmp_path, bare):
    kw = dict(role=role)
    env = dict(ENV)
    facts = _factories(tmp_path, bare)
    if scenario == "mock":
        spec = AgentSpec(**kw)
    elif scenario == "disabled":
        spec = AgentSpec(**kw, kind="real", enabled=False, credential_env=CRED[role], model=MODEL[role])
    elif scenario == "no_creds":
        spec, env = AgentSpec(**kw, kind="real", credential_env=CRED[role], model=MODEL[role]), {}
    elif scenario == "unpriced":
        spec = AgentSpec(**kw, kind="real", credential_env=CRED[role], model="no-such-model")
    elif scenario == "unconfigured":
        spec, facts = AgentSpec(**kw, kind="real", credential_env=CRED[role], model=MODEL[role]), real_factories()
    else:  # ready
        spec = AgentSpec(**kw, kind="real", credential_env=CRED[role], model=MODEL[role])
    return AgentRegistry([spec], all_mock_adapters(), env, TEST_PRICES, facts)


# ------------------------------------------------------------------ ROUTING matrix: mock and real, every role, every gate


@pytest.mark.parametrize("role", [r.value for r in Role])
def test_routing_matrix_every_role_and_scenario(repo, tmp_path, role):
    bare = make_bare_repo(tmp_path / "x")
    t = repo.create_task(role_contract(role if role != "ai_scientist" else "executor"), HUMAN).task
    t = t.model_copy(update={"assigned_role": Role(role)})
    expected = {
        "mock": None,
        "disabled": "disabled",
        "no_creds": "no configured credentials" if CRED[role] else "no handoff repository configured|handoff misconfigured|allowed_authors",
        "unpriced": "no entry in price table" if MODEL[role] else None,
        "unconfigured": ("no HTTP transport configured" if role in ("director", "planner") else
                         "loop is not configured" if role == "executor" else
                         "no handoff repository configured" if role == "chatgpt_work" else "no configured credentials|stub"),
        "ready": None,
    }
    for scenario, want in expected.items():
        reg = _registry(role, scenario, tmp_path / scenario, bare)
        if scenario == "no_creds" and role == "chatgpt_work":
            continue   # this role needs configuration, not a credential; covered by 'unconfigured'
        if scenario == "unpriced" and MODEL[role] is None:
            continue   # the handoff role calls no model, so it has no price gate
        if role == "ai_scientist" and scenario == "ready":
            want = SCIENTIST_STUB_REASON
        if role == "ai_scientist" and scenario == "unconfigured":
            want = "stub|no configured credentials"
        if want is None:
            adapter = reg.route(repo, t)
            assert adapter.is_mock is (scenario == "mock") and adapter.role.value == role
        else:
            n = len([e for e in repo.iter_events() if e.event_type == "agent.route_failed"])
            with pytest.raises(AdapterUnavailable, match=want.split("|")[0] if scenario != "ready" else "stub") as e:
                reg.route(repo, t)
            ev = [x for x in repo.iter_events() if x.event_type == "agent.route_failed"]
            assert len(ev) == n + 1 and ev[-1].payload["role"] == role and ev[-1].payload["reason"] == str(e.value)   # AUDITED
            assert not any(str(v) in str(e.value) for v in ENV.values())                                             # no key in the message


def test_ai_scientist_is_a_fail_closed_stub_even_with_credentials_price_and_everything(repo, tmp_path):
    bare = make_bare_repo(tmp_path / "x")
    reg = _registry("ai_scientist", "ready", tmp_path, bare)
    with pytest.raises(AdapterUnavailable, match="fail-closed stub"):
        reg.resolve("ai_scientist")
    assert "M5" in SCIENTIST_STUB_REASON and "ADR-0005" in SCIENTIST_STUB_REASON
    assert reg.real_roles() == {"ai_scientist"}


def test_worker_blocks_a_scientist_task_with_an_audited_error_and_never_runs_it(repo, tmp_path):
    from airos.domain.schemas import ApprovalRequirement, ApprovalType
    from tests.conftest import make_contract

    spec = AgentSpec(role="ai_scientist", kind="real", credential_env="TEST_OPENAI_KEY", model="gpt-test")
    w = real_world(repo, tmp_path, roles=(), extra_specs=[spec])
    w.registry._specs["ai_scientist"] = spec                          # replace the mock spec with the real (stub) one
    w.registry._factories = real_factories()
    t = approve_fully(repo, repo.create_task(make_contract(
        assigned_role=Role.AI_SCIENTIST, approval_requirements=[ApprovalRequirement(type=ApprovalType.EXECUTE, reason="x"),
                                                               ApprovalRequirement(type=ApprovalType.COST, reason="x")]), HUMAN).task)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.ADAPTER_UNAVAILABLE
    ev = [e for e in repo.iter_events() if e.event_type == "agent.route_failed"]
    assert len(ev) == 1 and "fail-closed stub" in ev[0].payload["reason"]
    assert repo.list_runs(t.task_id) == [] and [e for e in repo.list_ledger() if e.task_id == t.task_id] == []


# ------------------------------------------------------------------ BUDGET matrix: real roles default to $0


def _make_task(repo, role):
    return approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)


def _world(repo, tmp_path, role, *, env=None):
    fixtures = {"openai": [openai_fx(usage=TOKENS["director"])], "anthropic": [anthropic_fx(usage=TOKENS["planner"] if role == "planner" else TOKENS["executor"])]}
    bare = make_bare_repo(tmp_path / "xchg")
    cfg = HandoffConfig(repo_path=bare, allowed_authors=frozenset({"chatgpt-work@example.invalid"}), work_root=tmp_path / "cp")
    loop = ScriptedLoop(LoopResult("done", files=["out/result.md"], tokens_in=TOKENS["executor"][0], tokens_out=TOKENS["executor"][1]), writer=result_writer)
    return real_world(repo, tmp_path / "ws-root", fixtures=fixtures, env=env, handoff=cfg, loop=loop, roles=(role,)), bare


@pytest.mark.parametrize("role", ["director", "planner", "executor"])   # chatgpt_work needs configuration, not a credential (routing matrix)
def test_real_role_without_credentials_is_blocked_unavailable_audited_and_touches_no_budget(repo, tmp_path, role):
    w, _ = _world(repo, tmp_path, role, env={})
    t = _make_task(repo, role)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.ADAPTER_UNAVAILABLE
    assert [e.payload["role"] for e in repo.iter_events() if e.event_type == "agent.route_failed"] == [role]
    assert [e for e in repo.list_ledger() if e.task_id == t.task_id] == [] and repo.list_runs(t.task_id) == []
    assert all(not tr.calls for tr in w.transports.values())                                        # nothing was sent anywhere


@pytest.mark.parametrize("role", REAL_ROLES)
def test_real_role_has_a_zero_dollar_budget_until_a_human_grants_one(repo, tmp_path, role):
    w, _ = _world(repo, tmp_path, role)
    t = _make_task(repo, role)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.BUDGET_INSUFFICIENT
    st = repo.budget_status(f"role:{role}")
    assert (st.limit_usd_micro, st.limit_tokens, st.used_usd_micro) == (0, 0, 0)                    # the default is $0 / 0 tokens
    assert any("role:" in str(e.payload.get("reason")) for e in repo.iter_events() if e.event_type == "budget.reservation_refused")
    assert all(not tr.calls for tr in w.transports.values()) and w.loop.specs == []                  # adapter never reached
    assert repo.list_runs(t.task_id) == []


def _drive_real(repo, w, bare, tmp_path, role, t):
    """grant -> resume -> run (chatgpt_work needs the external answer in between)."""
    grant_role_budget(repo, t, role)
    cur = repo.get_task(t.task_id)
    repo.enqueue(cur.task_id, Event.UNBLOCK, HUMAN, cur.version)
    w.worker.tick()
    if role == "chatgpt_work":
        parked = repo.get_task(t.task_id)
        assert parked.state is TaskState.BLOCKED and parked.blocked_reason is BlockReason.NEEDS_INPUT          # waiting for the external side
        import hashlib

        req_sha = hashlib.sha256(git("show", f"main:requests/{t.task_id}/request.json", cwd=bare).encode()).hexdigest()
        external_workflow(bare, tmp_path, t.task_id, request_sha=req_sha, files={"notes.md": b"# external\n"})
        with pytest.raises(GuardFailed):                                                                      # the system cannot resume needs_input
            from airos.persistence.sqlite_repo import SYSTEM

            repo.enqueue(parked.task_id, Event.UNBLOCK, SYSTEM, parked.version, GuardContext(blocker_cleared=True))
        repo.enqueue(parked.task_id, Event.UNBLOCK, HUMAN, parked.version)
        w.worker.tick()
    return repo.get_task(t.task_id)


@pytest.mark.parametrize("role", REAL_ROLES)
def test_after_a_human_grant_the_real_role_runs_and_is_priced_from_the_table(repo, tmp_path, role):
    w, bare = _world(repo, tmp_path, role)
    t = _make_task(repo, role)
    w.worker.tick()                                                                                          # blocked on budget
    done = _drive_real(repo, w, bare, tmp_path, role, t)
    assert done.state is TaskState.REVIEW
    expected_cost = sum(TOKENS[role])                                                                        # 1 micro-USD per token in TEST_PRICES (0 for handoff)
    if role == "chatgpt_work":
        expected_cost = 0
    run = repo.list_runs(t.task_id)[-1]
    assert (run.cost_basis, run.cost_usd_micro, run.price_table_sha256) == ("priced", expected_cost, TEST_PRICES.sha256)
    assert run.model == (MODEL[role] or "handoff")
    assert repo.budget_status(f"role:{role}").used_usd_micro == expected_cost
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve" and e.task_id == t.task_id} <= {e.reserve_ref for e in ledger if e.kind.value == "release"}


# ------------------------------------------------------------------ PERMISSION matrix: same review/accept rules for real and mock


def _observe(repo, t, svc):
    from airos.persistence.sqlite_repo import SqliteRepository  # noqa: F401

    obs = {}
    cur = repo.get_task(t.task_id)
    obs["state"] = cur.state.value
    try:
        repo.transition(cur.task_id, Event.ACCEPT, EXECUTOR, cur.version)
        obs["producer_accepts"] = True
    except ActorNotAllowed:
        obs["producer_accepts"] = False
    try:
        repo.transition(cur.task_id, Event.ACCEPT, DIRECTOR, cur.version)
        obs["director_accepts"] = True
        return obs  # (consumed: the task is completed; the human check below would not apply)
    except GuardFailed:
        obs["director_accepts"] = False
    done = repo.transition(cur.task_id, Event.ACCEPT, HUMAN, cur.version)
    obs["human_accepts"] = done.state is TaskState.COMPLETED
    return obs


@pytest.mark.parametrize("role", REAL_ROLES)
def test_review_rules_are_identical_for_mock_and_real_adapters(repo, open_repo, tmp_path, role):
    # --- mock run
    from airos.agents.registry import AgentSpec as Spec
    from airos.agents.runner import FreshDirRunner
    from airos.agents.worker import Worker
    from airos.artifacts import ArtifactService, ArtifactStore

    mock_reg = AgentRegistry([Spec(role=r.value) for r in Role], {r: MockAdapter(r) for r in Role}, {})
    svc = ArtifactService(repo, ArtifactStore(tmp_path / "mc", tmp_path / "me"))
    mw = Worker(repo, mock_reg, FreshDirRunner(tmp_path / "mws"), svc, "w-mock")
    tm = _make_task(repo, role)
    mw.tick()
    mock_obs = _observe(repo, tm, svc)
    mock_art = [a for a in repo.list_artifacts() if a.producer_run_id == repo.list_runs(tm.task_id)[-1].run_id]
    # --- real run (fresh task, same repository)
    w, bare = _world(repo, tmp_path / "real", role)
    tr = _make_task(repo, role)
    w.worker.tick()
    done = _drive_real(repo, w, bare, tmp_path / "real", role, tr)
    assert done.state is TaskState.REVIEW
    real_obs = _observe(repo, tr, w.svc)
    real_art = [a for a in repo.list_artifacts() if a.producer_run_id == repo.list_runs(tr.task_id)[-1].run_id]
    assert real_obs == mock_obs, (role, real_obs, mock_obs)
    assert [a.is_mock for a in mock_art] == [True] and [a.is_mock for a in real_art] == [False]
    assert [a.trust_level for a in mock_art] == [a.trust_level for a in real_art]
    if role == "chatgpt_work":
        assert real_art[0].trust_level is TrustLevel.UNTRUSTED and real_art[0].artifact_class is ArtifactClass.EXTERNAL_HANDOFF
    assert all(svc.verify(a.artifact_id).ok for a in mock_art) and all(w.svc.verify(a.artifact_id).ok for a in real_art)   # each in its own store
    assert repo.verify_chain_with_anchors().ok


def test_a_real_adapter_cannot_read_what_its_role_may_not_read(repo, tmp_path):
    """The input reader is bound to the agent's own read permissions; a denial is audited and the task fails closed."""
    from airos.agents.adapters import MockAdapter as Base
    from airos.agents.runner import FreshDirRunner
    from airos.agents.worker import Worker
    from airos.artifacts import ArtifactService, ArtifactStore

    store = ArtifactStore(tmp_path / "c", tmp_path / "e")
    svc = ArtifactService(repo, store)
    held = svc.import_bytes(data=b"held-out labels", artifact_id="fe", logical_name="l.csv", media_type="text/csv",
                            artifact_class=ArtifactClass.FINAL_EVALUATION, human=HUMAN)

    class Snoop(Base):
        def execute(self, request, workdir, inputs=None):
            inputs(held.artifact_id)                     # a "real" adapter trying to read the held-out data
            raise AssertionError("must not get here")

    snoop = Snoop(Role.EXECUTOR)
    snoop.is_mock = False
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], all_mock_adapters(), {})
    original = reg.resolve
    reg.resolve = lambda role: snoop if role == "executor" else original(role)      # type: ignore[method-assign]
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), svc, "w")
    t = _make_task(repo, "executor")
    reads_before = list(store.reads)
    w.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.FAILED and cur.attempt_count == 1                  # policy violation: never retried
    assert store.reads == reads_before                                               # no held-out bytes were opened
    assert any(e.event_type == "authz.denied" and e.payload["action"] == "artifact.read:final_evaluation" for e in repo.iter_events())
    assert any(e.event_type == "adapter.response_rejected" and e.payload["category"] == "policy" for e in repo.iter_events())
    assert budget_sql.global_budget_id(repo.clock.now_ms())
