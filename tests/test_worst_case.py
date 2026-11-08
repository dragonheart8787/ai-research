"""Approval clarity: the contract's worst-case total (per-attempt cap x (1 + max_retries)) is derived, hashed, recorded
on every approval and shown by the API; changing the cap or the retries changes the hash and invalidates approvals."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from airos.api import app as api
from airos.domain.guards import GuardContext
from airos.domain.schemas import (
    ApprovalStatus,
    ApprovalType,
    ResourceLimits,
    TaskContract,
    contract_sha256,
)
from airos.domain.states import Event, TaskState
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import HUMAN, PLANNER, approve_fully, make_contract

TOKEN = "operator-" + "token-" + "W4c9Zk2Lm7"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("cap,retries,tokens", [(1_000_000, 2, 200_000), (0, 5, 1), (7, 0, 9), (50_000_000, 5, 2_000_000)])
def test_worst_case_total_is_cap_times_attempts(cap, retries, tokens):
    c = make_contract(resource_limits=ResourceLimits(max_usd_micro=cap, max_retries=retries, max_tokens=tokens),
                      approval_requirements=[{"type": "execute", "reason": "x"}, {"type": "cost", "reason": "x"}] if cap else
                      [{"type": "execute", "reason": "x"}])
    assert c.worst_case_total_usd_micro == cap * (1 + retries)
    assert c.worst_case_total_tokens == tokens * (1 + retries)
    dumped = c.model_dump(mode="json")
    assert dumped["worst_case_total_usd_micro"] == cap * (1 + retries)          # it is part of what gets hashed


def _hash_with(**limits):
    base = dict(max_usd_micro=1_000_000, max_retries=2)
    base.update(limits)
    return contract_sha256(make_contract(idempotency_key="idem-key-fixed-0000001", resource_limits=ResourceLimits(**base)))


def test_changing_the_cap_or_the_retries_changes_the_hash():
    h = _hash_with()
    assert h == _hash_with()
    assert _hash_with(max_usd_micro=1_000_001) != h
    assert _hash_with(max_retries=3) != h
    assert _hash_with(max_retries=1) != h
    assert _hash_with(max_tokens=199_999) != h


def test_a_stated_worst_case_must_equal_the_derived_one():
    c = make_contract()
    d = c.model_dump(mode="json")
    assert TaskContract.model_validate(d) == c                                   # round trip incl. the derived fields
    for key in ("worst_case_total_usd_micro", "worst_case_total_tokens"):
        with pytest.raises(ValidationError, match="does not equal"):
            TaskContract.model_validate({**d, key: d[key] - 1})                   # a lying worst-case is rejected
    d2 = {k: v for k, v in d.items() if not k.startswith("worst_case")}
    assert TaskContract.model_validate(d2).worst_case_total_usd_micro == c.worst_case_total_usd_micro


def test_every_approval_records_the_worst_case_and_it_is_immutable(repo, db_path):
    t = repo.create_task(make_contract(resource_limits=ResourceLimits(max_usd_micro=500_000, max_retries=3)), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    bi = repo.request_budget_increase(t.task_id, f"root:{t.root_task_id}", 10, 10, PLANNER)
    for a in (ap, bi):
        assert (a.worst_case_total_usd_micro, a.worst_case_total_tokens) == (2_000_000, 800_000)
        assert repo.get_approval(a.approval_id) == a
    raw = sqlite3.connect(db_path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw.execute("UPDATE approvals SET worst_case_usd_micro=1 WHERE approval_id=?", (ap.approval_id,))
    raw.close()


@pytest.mark.parametrize("change", [dict(max_retries=3), dict(max_retries=0), dict(max_usd_micro=1_500_000)])
def test_changing_retries_or_cap_invalidates_approvals_and_new_ones_show_the_new_worst_case(repo, change):
    t = approve_fully(repo, repo.create_task(make_contract(), HUMAN).task)
    old = repo.list_approvals(t.task_id)
    assert {a.status for a in old} == {ApprovalStatus.GRANTED}
    limits = {**t.contract.resource_limits.model_dump(), **change}
    amended = make_contract(idempotency_key=t.idempotency_key, resource_limits=ResourceLimits(**limits))
    assert contract_sha256(amended) != t.contract_sha256
    t2 = repo.amend_task(t.task_id, amended, t.version, HUMAN)
    assert t2.state is TaskState.AWAITING_APPROVAL and t2.contract_version == 2
    assert {a.status for a in repo.list_approvals(t.task_id) if a.task_version == 1} == {ApprovalStatus.INVALIDATED}
    new = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    assert new.worst_case_total_usd_micro == amended.worst_case_total_usd_micro != t.contract.worst_case_total_usd_micro
    assert new.contract_sha256 == contract_sha256(amended)


def test_api_shows_the_worst_case_total_and_binding(repo):
    c = TestClient(api.create_app(repo, {api.TOKEN_ENV: TOKEN}))
    contract = make_contract(resource_limits=ResourceLimits(max_usd_micro=250_000, max_retries=4))
    created = c.post("/v1/tasks", json=contract.model_dump(mode="json"), headers=AUTH).json()
    assert created["worst_case_total_usd_micro"] == 1_250_000 and created["contract_sha256"] == contract_sha256(contract)
    liar = contract.model_dump(mode="json") | {"worst_case_total_usd_micro": 1}
    assert c.post("/v1/tasks", json=liar, headers=AUTH).status_code == 422
    tid = created["task_id"]
    t = repo.get_task(tid)
    t = repo.transition(tid, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(tid, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    ap = repo.request_approval(tid, ApprovalType.COST, SYSTEM)
    shown = c.get(f"/v1/approvals/{ap.approval_id}", headers=AUTH).json()
    assert shown["worst_case_total_usd_micro"] == 1_250_000 and shown["worst_case_total_tokens"] == 1_000_000
    assert shown["contract_sha256"] == created["contract_sha256"] and shown["task_version"] == 1
    decided = c.post(f"/v1/approvals/{ap.approval_id}/decision", json={"grant": True}, headers=AUTH).json()
    assert decided["worst_case_total_usd_micro"] == 1_250_000 and decided["status"] == "granted"
    assert c.get(f"/v1/tasks/{tid}", headers=AUTH).json()["approvals"][0]["worst_case_total_usd_micro"] == 1_250_000
