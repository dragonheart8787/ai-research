"""Real adapters are held to the SAME matrices as mocks: routing, budget, and permission outcomes.
(Real = fixture-backed behind the CliRunner seam; nothing here touches a network, a container, or a provider CLI.)"""
from __future__ import annotations

import hashlib
from datetime import date

import pytest

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.real.claude_executor import LoopResult
from airos.agents.real.cli import FixtureCliRunner
from airos.agents.real.factory import SCIENTIST_STUB_REASON, real_factories
from airos.agents.real.handoff import HandoffConfig
from airos.agents.registry import AgentRegistry, AgentSpec, RegistryPolicy
from airos.domain.guards import GuardContext
from airos.domain.limits import Limits
from airos.domain.schemas import ArtifactClass, Role, TrustLevel
from airos.domain.states import BlockReason, Event, TaskState
from airos.errors import ActorNotAllowed, AdapterUnavailable, GuardFailed
from tests.conftest import DIRECTOR, EXECUTOR, HUMAN, TEST_PRICES, approve_fully
from tests.real_helpers import (
    DIRECTOR_MODEL,
    ENV,
    EXECUTOR_ENV,
    PLANNER_ENV,
    POLICY,
    ScriptedLoop,
    claude_fx,
    codex_exec_fx,
    codex_status_fx,
    external_workflow,
    git,
    make_bare_repo,
    plan_json,
    pool_used,
    real_world,
    result_writer,
    role_contract,
)

REAL_ROLES = ["director", "planner", "executor", "chatgpt_work"]
CRED = {"director": None, "planner": PLANNER_ENV, "executor": EXECUTOR_ENV, "chatgpt_work": None, "ai_scientist": EXECUTOR_ENV}
MODEL = {"director": DIRECTOR_MODEL, "planner": "claude-opus-5-5", "executor": "claude-sonnet-5-5", "chatgpt_work": None,
         "ai_scientist": "claude-sonnet-5-5"}
MODE = {"chatgpt_work": "manual_handoff"}
TOKENS = {"director": (120, 30), "planner": (20, 8), "executor": (50, 10), "chatgpt_work": (0, 0)}
OPTS = {"director": {"no_api_key_attested": "1"}}


def _spec(role, **over):
    base = dict(role=role, kind="real", credential_env=CRED[role], model=MODEL[role], options=OPTS.get(role, {}),
                auth_mode=MODE.get(role, "subscription_oauth"))
    base.update(over)
    return AgentSpec(**base)


def _factories(tmp_path, bare):
    return real_factories(cli_runner=FixtureCliRunner([]), executor_loop=ScriptedLoop(),
                          handoff=HandoffConfig(repo_path=bare, allowed_authors=frozenset({"a@example.invalid"}), work_root=tmp_path / "cp"))


def _registry(role, scenario, tmp_path, bare):
    env, facts, policy = dict(ENV), _factories(tmp_path, bare), POLICY
    if scenario == "mock":
        spec = AgentSpec(role=role)
    elif scenario == "disabled":
        spec = _spec(role, enabled=False)
    elif scenario == "no_creds":
        spec, env = _spec(role), {}
    elif scenario == "unpriced":
        spec = _spec(role, model="no-such-model")
        policy = RegistryPolicy(usage_credits_attested_off=True, attested_on=date(2026, 11, 1), today=lambda: date(2026, 11, 7),
                                model_allowlist={"planner": frozenset({"no-such-model"}), "executor": frozenset({"no-such-model"})})
    elif scenario == "unconfigured":
        spec, facts = _spec(role), real_factories()
    else:  # ready
        spec = _spec(role)
    return AgentRegistry([spec], all_mock_adapters(), env, TEST_PRICES, facts, policy=policy)


# ------------------------------------------------------------------ ROUTING matrix: mock and real, every role, every gate


@pytest.mark.parametrize("role", [r.value for r in Role])
def test_routing_matrix_every_role_and_scenario(repo, tmp_path, role):
    bare = make_bare_repo(tmp_path / "x")
    t = repo.create_task(role_contract(role if role != "ai_scientist" else "executor"), HUMAN).task
    t = t.model_copy(update={"assigned_role": Role(role)})
    expected = {
        "mock": None,
        "disabled": "disabled",
        "no_creds": "no configured credentials" if CRED[role] else None,
        "unpriced": "no entry in price table" if MODEL[role] else None,
        "unconfigured": ("no sandboxed CLI runner configured" if role in ("director", "planner") else
                         "loop is not configured" if role == "executor" else
                         "no handoff repository configured" if role == "chatgpt_work" else "stub"),
        "ready": None,
    }
    for scenario, want in expected.items():
        if scenario in ("no_creds", "unpriced") and want is None:
            continue   # this role needs no credential / calls no model: there is no such gate to test
        reg = _registry(role, scenario, tmp_path / scenario, bare)
        if role == "ai_scientist" and scenario == "ready":
            want = "fail-closed stub"
        if want is None:
            adapter = reg.route(repo, t)
            assert adapter.is_mock is (scenario == "mock") and adapter.role.value == role
        else:
            n = len([e for e in repo.iter_events() if e.event_type == "agent.route_failed"])
            with pytest.raises(AdapterUnavailable, match=want) as e:
                reg.route(repo, t)
            ev = [x for x in repo.iter_events() if x.event_type == "agent.route_failed"]
            assert len(ev) == n + 1 and ev[-1].payload["role"] == role and ev[-1].payload["reason"] == str(e.value)   # AUDITED
            assert not any(str(v) in str(e.value) for v in ENV.values())                                             # no token in the message


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

    w = real_world(repo, tmp_path, roles=(), extra_specs=[])
    w.registry._specs["ai_scientist"] = _spec("ai_scientist")        # replace the mock spec with the real (stub) one
    w.registry._factories = real_factories()
    t = approve_fully(repo, repo.create_task(make_contract(
        assigned_role=Role.AI_SCIENTIST, approval_requirements=[ApprovalRequirement(type=ApprovalType.EXECUTE, reason="x"),
                                                               ApprovalRequirement(type=ApprovalType.COST, reason="x")]), HUMAN).task)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.ADAPTER_UNAVAILABLE
    ev = [e for e in repo.iter_events() if e.event_type == "agent.route_failed"]
    assert len(ev) == 1 and ("stub" in ev[0].payload["reason"])
    assert repo.list_runs(t.task_id) == [] and [e for e in repo.list_ledger() if e.task_id == t.task_id] == []


# ------------------------------------------------------------------ BUDGET matrix: the agent_sdk_credit pool and its allocations


def _make_task(repo, role):
    return approve_fully(repo, repo.create_task(role_contract(role), HUMAN).task)


def _world(repo, tmp_path, role, *, env=None):
    fixtures = [codex_status_fx(), codex_exec_fx(usage=TOKENS["director"]), claude_fx(plan_json(2), usage=TOKENS["planner"])]
    bare = make_bare_repo(tmp_path / "xchg")
    cfg = HandoffConfig(repo_path=bare, allowed_authors=frozenset({"chatgpt-work@example.invalid"}), work_root=tmp_path / "cp")
    loop = ScriptedLoop(LoopResult("done", files=["out/result.md"], tokens_in=TOKENS["executor"][0], tokens_out=TOKENS["executor"][1],
                                   auth_source="oauth"), writer=result_writer)
    return real_world(repo, tmp_path / "ws-root", fixtures=fixtures, env=env, handoff=cfg, loop=loop, roles=(role,)), bare


@pytest.mark.parametrize("role", ["planner", "executor"])   # the others need configuration or no credential (routing matrix)
def test_real_role_without_credentials_is_blocked_unavailable_audited_and_touches_no_budget(real_repo, tmp_path, role):
    repo = real_repo
    w, _ = _world(repo, tmp_path, role, env={})
    t = _make_task(repo, role)
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.ADAPTER_UNAVAILABLE
    assert [e.payload["role"] for e in repo.iter_events() if e.event_type == "agent.route_failed"] == [role]
    assert [e for e in repo.list_ledger() if e.task_id == t.task_id] == [] and repo.list_runs(t.task_id) == []
    assert w.runner.calls == [] and w.loop.specs == []                                              # nothing was run anywhere


def test_the_director_cannot_run_until_the_human_sets_a_run_cap(repo, tmp_path):
    w, _ = _world(repo, tmp_path, "director")                                                     # default Limits: 0 runs per day
    t = _make_task(repo, "director")
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.BUDGET_INSUFFICIENT
    assert any("director run cap" in str(e.payload.get("reason")) for e in repo.iter_events() if e.event_type == "budget.reservation_refused")
    assert w.runner.calls == [] and repo.list_runs(t.task_id) == []


def test_a_zero_allocation_blocks_as_credit_exhausted_and_never_reaches_the_adapter(open_repo, tmp_path):
    repo = open_repo(limits=Limits(role_allocations_usd_micro={"executor": 0, "planner": 3_000_000}))
    w, _ = _world(repo, tmp_path, "executor")
    t = _make_task(repo, "executor")
    w.worker.tick()
    cur = repo.get_task(t.task_id)
    assert cur.state is TaskState.BLOCKED and cur.blocked_reason is BlockReason.CREDIT_EXHAUSTED
    assert w.loop.specs == [] and repo.list_runs(t.task_id) == []
    assert cur.attempt_count == 0                                                                  # never counted as an attempt


def _drive_real(repo, w, bare, tmp_path, role, t):
    """run (chatgpt_work needs the external answer in between)."""
    w.worker.tick()
    if role == "chatgpt_work":
        parked = repo.get_task(t.task_id)
        assert parked.state is TaskState.BLOCKED and parked.blocked_reason is BlockReason.NEEDS_INPUT          # waiting for the external side
        req_sha = hashlib.sha256(git("show", f"main:requests/{t.task_id}/request.json", cwd=bare).encode()).hexdigest()
        external_workflow(bare, tmp_path, t.task_id, request_sha=req_sha, files={"notes.md": b"# external\n"})
        with pytest.raises(GuardFailed):                                                                      # the system cannot resume needs_input
            from airos.persistence.sqlite_repo import SYSTEM

            repo.enqueue(parked.task_id, Event.UNBLOCK, SYSTEM, parked.version, GuardContext(blocker_cleared=True))
        repo.enqueue(parked.task_id, Event.UNBLOCK, HUMAN, parked.version)
        w.worker.tick()
    return repo.get_task(t.task_id)


@pytest.mark.parametrize("role", REAL_ROLES)
def test_the_real_role_runs_and_is_priced_from_the_table_and_books_the_pool(real_repo, tmp_path, role):
    repo = real_repo
    w, bare = _world(repo, tmp_path, role)
    t = _make_task(repo, role)
    done = _drive_real(repo, w, bare, tmp_path, role, t)
    assert done.state is TaskState.REVIEW
    expected_cost = 0 if role == "chatgpt_work" else sum(TOKENS[role])                                      # 1 micro-USD per token in TEST_PRICES
    run = repo.list_runs(t.task_id)[-1]
    assert (run.cost_basis, run.cost_usd_micro, run.price_table_sha256) == ("priced", expected_cost, TEST_PRICES.sha256)
    assert run.model == (MODEL[role] or "handoff")
    if role in ("planner", "executor"):
        assert pool_used(repo) == pool_used(repo, role) == expected_cost                                    # pool and allocation both charged
    else:
        assert pool_used(repo) == 0                                                                         # director / handoff never touch the dollar pool
    ledger = repo.list_ledger()
    assert {e.entry_id for e in ledger if e.kind.value == "reserve" and e.task_id == t.task_id} <= {e.reserve_ref for e in ledger if e.kind.value == "release"}


# ------------------------------------------------------------------ PERMISSION matrix: same review/accept rules for real and mock


def _observe(repo, t, svc):
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
        return obs
    except GuardFailed:
        obs["director_accepts"] = False
    done = repo.transition(cur.task_id, Event.ACCEPT, HUMAN, cur.version)
    obs["human_accepts"] = done.state is TaskState.COMPLETED
    return obs


@pytest.mark.parametrize("role", REAL_ROLES)
def test_review_rules_are_identical_for_mock_and_real_adapters(real_repo, tmp_path, role):
    repo = real_repo
    from airos.agents.runner import FreshDirRunner
    from airos.agents.worker import Worker
    from airos.artifacts import ArtifactService, ArtifactStore

    mock_reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], {r: MockAdapter(r) for r in Role}, {})
    svc = ArtifactService(repo, ArtifactStore(tmp_path / "mc", tmp_path / "me"))
    mw = Worker(repo, mock_reg, FreshDirRunner(tmp_path / "mws"), svc, "w-mock")
    tm = _make_task(repo, role)
    mw.tick()
    mock_obs = _observe(repo, tm, svc)
    mock_art = [a for a in repo.list_artifacts() if a.producer_run_id == repo.list_runs(tm.task_id)[-1].run_id]
    w, bare = _world(repo, tmp_path / "real", role)
    tr = _make_task(repo, role)
    done = _drive_real(repo, w, bare, tmp_path / "real", role, tr)
    assert done.state is TaskState.REVIEW
    real_obs = _observe(repo, tr, w.svc)
    real_art = [a for a in repo.list_artifacts() if a.producer_run_id == repo.list_runs(tr.task_id)[-1].run_id]
    assert real_obs == mock_obs, (role, real_obs, mock_obs)
    assert [a.is_mock for a in mock_art] == [True] and [a.is_mock for a in real_art] == [False]
    assert [a.trust_level for a in mock_art] == [a.trust_level for a in real_art]
    if role == "chatgpt_work":
        assert real_art[0].trust_level is TrustLevel.UNTRUSTED and real_art[0].artifact_class is ArtifactClass.EXTERNAL_HANDOFF
    assert all(svc.verify(a.artifact_id).ok for a in mock_art) and all(w.svc.verify(a.artifact_id).ok for a in real_art)
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
    snoop.runner_kind = "fixture"                    # type: ignore[attr-defined]  (passes the sandbox gate; the read itself must still be denied)
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
