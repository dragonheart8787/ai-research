"""Cost from tokens: USD = tokens x pinned price table, computed by the control plane. Adapter-reported USD is ignored
(only cross-checked). Unknown model / missing price settles at worst case and raises an audit flag."""
from __future__ import annotations

import copy
import json

import pytest

from airos.agents.adapters import all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.domain.schemas import ResourceUsage, Role
from airos.domain.states import RunState
from airos.errors import AdapterUnavailable, SchemaMismatch
from airos.pricing import DEFAULT_PRICE_TABLE_SHA256, PriceTable
from tests.conftest import HUMAN, make_contract, to_running

USD = 1_000_000
FAKE_KEY = "fake-" + "price-credential-" + "4455667"


def _u(model, tin=0, tout=0, reported=0):
    return ResourceUsage(tokens_in=tin, tokens_out=tout, usd_micro=reported, wall_ms=1, measured_by="adapter", model=model)


def _table(**models):
    return PriceTable.from_dict({"schema": "airos-prices/1", "version": "t", "currency": "USD",
                                 "unit": "usd_micro_per_million_tokens",
                                 "models": {k: {"input": v[0], "output": v[1]} for k, v in models.items()}})


# ------------------------------------------------------------------ the table itself


def test_cost_is_exact_integer_arithmetic_rounded_up():
    t = _table(m=(3_000_000, 15_000_000))                      # 3 / 15 USD per million tokens, in micro-USD
    assert t.cost_usd_micro("m", 1_000_000, 1_000_000) == 18_000_000
    assert t.cost_usd_micro("m", 1, 0) == 3 and t.cost_usd_micro("m", 0, 1) == 15
    assert t.cost_usd_micro("m", 0, 0) == 0
    cheap = _table(m=(1, 1))                                    # 1 micro-USD per million tokens: 1 token rounds UP to 1
    assert cheap.cost_usd_micro("m", 1, 0) == 1 and cheap.cost_usd_micro("m", 1_000_000, 0) == 1 \
        and cheap.cost_usd_micro("m", 1_000_001, 0) == 2
    assert t.cost_usd_micro("nope", 5, 5) is None and t.cost_usd_micro(None, 5, 5) is None
    assert t.cost_usd_micro("m", 10**15, 10**15) == (10**15 * 3_000_000 + 10**15 * 15_000_000) // 1_000_000   # exact, no float error


def test_table_is_validated_hashed_and_pinned():
    data = {"schema": "airos-prices/1", "version": "v", "currency": "USD", "unit": "usd_micro_per_million_tokens",
            "models": {"m": {"input": 1, "output": 2}}}
    t1 = PriceTable.from_dict(data)
    assert t1 == PriceTable.from_dict(copy.deepcopy(data)) and len(t1.sha256) == 64
    changed = copy.deepcopy(data)
    changed["models"]["m"]["output"] = 3
    assert PriceTable.from_dict(changed).sha256 != t1.sha256
    with pytest.raises(SchemaMismatch, match="pinned"):
        PriceTable.from_dict(changed, expected_sha256=t1.sha256)
    for bad in ({"input": -1, "output": 0}, {"input": 1.5, "output": 0}, {"input": True, "output": 0}, {"input": 1}):
        with pytest.raises(SchemaMismatch):
            PriceTable.from_dict({**data, "models": {"m": bad}})
    for key in ("schema", "unit", "currency"):
        with pytest.raises(SchemaMismatch):
            PriceTable.from_dict({**data, key: "x"})


def test_packaged_default_table_is_pinned_and_contains_no_invented_prices(tmp_path):
    d = PriceTable.default()
    assert d.sha256 == DEFAULT_PRICE_TABLE_SHA256 and set(d.models) == {"mock"}
    assert d.cost_usd_micro("mock", 10**6, 10**6) == 0
    # editing the shipped file without bumping the pin fails closed
    from importlib import resources

    raw = json.loads(resources.files("airos.pricing_data").joinpath("prices-v1.json").read_text())
    raw["models"]["sneaky"] = {"input": 0, "output": 0}
    p = tmp_path / "prices.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(SchemaMismatch, match="pinned"):
        PriceTable.load(p, expected_sha256=DEFAULT_PRICE_TABLE_SHA256)


# ------------------------------------------------------------------ settlement uses the table, not the adapter


def test_usd_is_computed_from_tokens_and_adapter_reported_usd_is_ignored(open_repo):
    repo = open_repo(price_table=_table(m=(2_000_000, 4_000_000)))
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    # adapter claims it cost 1 micro-USD; tokens say 100*2 + 50*4 = 400
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_u("m", 100, 50, reported=1))
    run = repo.list_runs(t.task_id)[0]
    assert (run.cost_usd_micro, run.cost_basis, run.model) == (400, "priced", "m")
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 400
    settle = [e for e in repo.list_ledger() if e.kind.value == "settle" and e.budget_id == f"task:{t.task_id}"]
    assert [e.usd_micro for e in settle] == [400]


def test_inflated_adapter_usd_cannot_drain_or_hide_spend(open_repo):
    repo = open_repo(price_table=_table(m=(1_000_000, 1_000_000)))
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_u("m", 10, 0, reported=10**12))
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 10
    flags = [e for e in repo.iter_events() if e.event_type == "usage.divergence"]
    assert len(flags) == 1 and flags[0].payload["adapter_reported_usd_micro"] == 10**12 and flags[0].payload["computed_usd_micro"] == 10
    assert flags[0].payload["used"] == "computed"


@pytest.mark.parametrize("reported,flagged", [(100, False), (105, False), (95, False), (106, True), (94, True), (0, True)])
def test_divergence_flag_threshold_is_five_percent(open_repo, reported, flagged):
    repo = open_repo(price_table=_table(m=(1_000_000, 0)))
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_u("m", 100, 0, reported=reported))
    assert bool([e for e in repo.iter_events() if e.event_type == "usage.divergence"]) is flagged
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 100            # computed in every case


@pytest.mark.parametrize("model,reason", [("unpriced-model", "unknown_model_no_price"), (None, "missing_model")])
def test_unknown_or_missing_model_settles_at_worst_case_and_raises_an_audit_flag(repo, model, reason):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_u(model, 5, 5, reported=0))
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == USD             # the FULL reservation, not 0
    [flag] = [e for e in repo.iter_events() if e.event_type == "pricing.flag"]
    assert flag.payload["reason"] == reason and flag.payload["settled"] == "worst_case" and flag.task_id == t.task_id
    run = repo.list_runs(t.task_id)[0]
    assert (run.cost_basis, run.cost_usd_micro) == ("worst_case", None)
    settled = [e for e in repo.iter_events() if e.event_type == "budget.settled"][-1]
    assert settled.payload["worst_case"] is True


def test_no_usage_at_all_is_still_worst_case(repo):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True)
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == USD
    assert repo.list_runs(t.task_id)[0].cost_basis == "worst_case"


def test_run_record_pins_the_price_table_version_and_hash_in_force(open_repo, db_path, clock):
    from airos.persistence.sqlite_repo import SqliteRepository

    a = open_repo(price_table=_table(m=(1_000_000, 0)))
    t1, r1 = to_running(a, a.create_task(make_contract(), HUMAN).task)
    a.complete_run(r1, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_u("m", 7))
    run1 = a.list_runs(t1.task_id)[0]
    assert (run1.price_table_version, run1.price_table_sha256) == (a.price_table.version, a.price_table.sha256)
    # a different table later: the OLD run keeps the hash it was priced with
    b = SqliteRepository(db_path, clock=clock, price_table=PriceTable.from_dict(
        {"schema": "airos-prices/1", "version": "t2", "currency": "USD", "unit": "usd_micro_per_million_tokens",
         "models": {"m": {"input": 9_000_000, "output": 0}}}))
    assert b.list_runs(t1.task_id)[0].price_table_sha256 == a.price_table.sha256 != b.price_table.sha256
    assert any(e.event_type == "budget.settled" and e.payload["price_table_sha256"] == a.price_table.sha256 for e in b.iter_events())
    b.close()


def test_mock_adapters_are_priced_at_zero_by_the_default_table(repo):
    t, rid = to_running(repo, repo.create_task(make_contract(), HUMAN).task)
    repo.complete_run(rid, "w1", RunState.SUCCEEDED, verifier_passed=True, usage=_u("mock", 10**9, 10**9, reported=0))
    assert repo.budget_status(f"task:{t.task_id}").used_usd_micro == 0


# ------------------------------------------------------------------ a real adapter cannot be enabled without a price


def test_real_adapter_without_a_price_entry_fails_closed_even_with_credentials(repo):
    table = _table(**{"priced-model": (1, 1)})
    env = {"K": FAKE_KEY}
    for model in (None, "", "unpriced-model", "PRICED-MODEL"):
        reg = AgentRegistry([AgentSpec(role="executor", kind="real", credential_env="K", model=model)], all_mock_adapters(), env, table)
        with pytest.raises(AdapterUnavailable, match="no entry in price table"):
            reg.resolve("executor")
    # with a price entry the NEXT gate is reached (no implementation in M2/this registry): still fail-closed
    reg = AgentRegistry([AgentSpec(role="executor", kind="real", credential_env="K", model="priced-model")], all_mock_adapters(), env, table)
    with pytest.raises(AdapterUnavailable, match="no real adapter implementation"):
        reg.resolve("executor")
    # order of gates: credentials are checked before the price
    reg = AgentRegistry([AgentSpec(role="executor", kind="real", credential_env="K", model="priced-model")], all_mock_adapters(), {}, table)
    with pytest.raises(AdapterUnavailable, match="no configured credentials"):
        reg.resolve("executor")
    assert Role.EXECUTOR
