"""Minimal API: operator-token mutations, read-only status, loopback bind by default, token never reaches agents."""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from airos.agents.adapters import all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.api import app as api
from airos.domain.guards import GuardContext
from airos.domain.schemas import ApprovalType, Role
from airos.domain.states import Event, TaskState
from airos.persistence.sqlite_repo import SYSTEM
from tests.conftest import HUMAN, PLANNER, make_contract

TOKEN = "operator-" + "token-" + "Q7x9Lm2Zk4"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(repo):
    return TestClient(api.create_app(repo, {api.TOKEN_ENV: TOKEN}))


def _body(contract):
    return contract.model_dump(mode="json")


def test_mutations_require_the_operator_token(client, repo):
    c = make_contract()
    for headers, code in (({}, 401), ({"Authorization": "Bearer wrong"}, 403), ({"Authorization": "Basic abc"}, 401),
                          ({"Authorization": f"Bearer {TOKEN}x"}, 403)):
        assert client.post("/v1/tasks", json=_body(c), headers=headers).status_code == code
    assert repo.list_tasks() == []
    r = client.post("/v1/tasks", json=_body(c), headers=AUTH)
    assert r.status_code == 201 and r.json()["created"] is True
    again = client.post("/v1/tasks", json=_body(c), headers=AUTH)          # idempotent duplicate through the API too
    assert again.json()["task_id"] == r.json()["task_id"] and again.json()["created"] is False


def test_every_non_get_route_is_operator_protected_and_get_routes_never_mutate(client, repo):
    eps = _endpoints(client.app)
    mutating = [(m, p) for m, p in eps if m != "GET"]
    assert {p for _, p in mutating} == {"/v1/tasks", "/v1/approvals/{approval_id}/decision", "/v1/tasks/{task_id}/cancel"}
    assert all(m == "POST" for m, _ in mutating)
    for _, p in mutating:
        assert client.post(_concrete(p), json={}).status_code == 401
    t = repo.create_task(make_contract(), HUMAN).task
    head = repo.head()
    for m, p in eps:
        if m == "GET" and p != "/healthz":
            path = p.replace("{task_id}", t.task_id).replace("{budget_id}", f"task:{t.task_id}")
            assert client.get(path, headers=AUTH).status_code == 200, path
    assert repo.head() == head                                              # reading changed nothing
    assert client.put("/v1/tasks", json={}).status_code == 405 and client.delete("/v1/status").status_code == 405


def test_unconfigured_token_fails_closed(repo):
    c = TestClient(api.create_app(repo, {}))
    assert c.post("/v1/tasks", json=_body(make_contract()), headers=AUTH).status_code == 503
    assert c.post("/v1/tasks", json=_body(make_contract())).status_code == 503
    empty = TestClient(api.create_app(repo, {api.TOKEN_ENV: ""}))
    assert empty.post("/v1/tasks", json=_body(make_contract()), headers={"Authorization": "Bearer "}).status_code in (401, 503)
    assert c.get("/v1/status", headers=AUTH).status_code == 503                # reads fail closed too when unconfigured
    assert c.get("/healthz").status_code == 204                                 # the one public endpoint


def test_approval_decision_and_cancel_through_the_api(client, repo):
    t = repo.create_task(make_contract(), HUMAN).task
    t = repo.transition(t.task_id, Event.PLAN, PLANNER, t.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    ap = repo.request_approval(t.task_id, ApprovalType.COST, SYSTEM)
    r = client.post(f"/v1/approvals/{ap.approval_id}/decision", json={"grant": True, "reason": "ok"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "granted"
    assert repo.list_approvals(t.task_id)[0].decided_by == "operator"
    assert client.post(f"/v1/approvals/{ap.approval_id}/decision", json={"grant": True}, headers=AUTH).status_code == 409
    assert client.post(f"/v1/tasks/{t.task_id}/cancel", json={"expected_version": 1}, headers=AUTH).status_code == 409  # stale
    out = client.post(f"/v1/tasks/{t.task_id}/cancel", json={"expected_version": t.version}, headers=AUTH)
    assert out.status_code == 200 and out.json()["state"] == TaskState.CANCELLED.value
    assert client.post("/v1/tasks/nope/cancel", json={"expected_version": 1}, headers=AUTH).status_code == 404
    assert client.post("/v1/tasks/x/cancel", json={"expected_version": 1, "extra": 1}, headers=AUTH).status_code == 422


def test_status_and_audit_endpoints(client, repo):
    t = repo.create_task(make_contract(), HUMAN).task
    s = client.get("/v1/status", headers=AUTH).json()
    assert s["tasks_by_state"] == {"draft": 1} and s["audit_chain_ok"] is True
    v = client.get("/v1/audit/verify", headers=AUTH).json()
    assert v["ok"] is True and v["head_hash"] == repo.head().entry_hash
    assert t.task_id in client.get("/", headers=AUTH).text and "read-only" in client.get("/", headers=AUTH).text


def test_token_is_never_returned_stored_or_logged(client, repo, db_path):
    client.post("/v1/tasks", json=_body(make_contract()), headers=AUTH)
    client.post("/v1/tasks", json=_body(make_contract()), headers={"Authorization": "Bearer " + TOKEN + "zz"})
    repo.close()
    blob = b"".join(Path(str(db_path) + s).read_bytes() for s in ("", "-wal") if Path(str(db_path) + s).exists())
    assert TOKEN.encode() not in blob and hashlib.sha256(TOKEN.encode()).hexdigest().encode() not in blob


def test_agents_never_receive_the_operator_token(repo):
    env = {api.TOKEN_ENV: TOKEN, "EXEC_KEY": "k" * 12, "PATH": "/usr/bin"}
    agent_env = api.agent_environ(env)
    assert api.TOKEN_ENV not in agent_env and agent_env["EXEC_KEY"] == "k" * 12 and env[api.TOKEN_ENV] == TOKEN
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], all_mock_adapters(), agent_env)
    assert TOKEN not in repr(vars(reg))
    sig = inspect.signature(api.create_app)
    assert list(sig.parameters) == ["repo", "environ"]                      # the only consumer of the token is the app


def test_binds_loopback_by_default_and_refuses_other_hosts(repo):
    assert api.DEFAULT_HOST == "127.0.0.1"
    assert inspect.signature(api.serve).parameters["host"].default == "127.0.0.1"
    app = api.create_app(repo, {api.TOKEN_ENV: TOKEN})
    for host in ("0.0.0.0", "192.168.1.5", "example.com", ""):       # noqa: S104
        with pytest.raises(ValueError, match="loopback"):
            api.serve(app, host=host)


def _endpoints(app):
    """Every (METHOD, path) the app serves, from the OpenAPI model PLUS the raw included routers (so a route with
    include_in_schema=False cannot hide)."""
    found = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops if m.upper() not in ("HEAD", "OPTIONS")}
    for r in app.routes:
        inner = getattr(r, "original_router", None)
        for rr in (inner.routes if inner is not None else [r]):
            for m in getattr(rr, "methods", ()) or ():
                if m not in ("HEAD", "OPTIONS"):
                    found.add((m, rr.path))
    return sorted(found)


def _concrete(path):
    import re as _re

    return _re.sub(r"\{[^}]+\}", "x", path)


def test_every_registered_route_rejects_missing_or_wrong_token_except_healthz(repo):
    """Enumerates what the app actually serves (not a hand-kept list): a route added later is covered automatically."""
    app = api.create_app(repo, {api.TOKEN_ENV: TOKEN})
    c = TestClient(app)
    eps = _endpoints(app)
    assert ("GET", "/healthz") in eps and ("GET", "/") in eps and ("POST", "/v1/tasks") in eps
    checked = 0
    for method, route_path in eps:
        if route_path == "/healthz":
            continue
        path = _concrete(route_path)
        for headers, code in (({}, 401), ({"Authorization": "Bearer wrong"}, 403), ({"Authorization": "Basic x"}, 401),
                              ({"Authorization": f"Bearer {TOKEN}x"}, 403)):
            resp = c.request(method, path, headers=headers, json={} if method == "POST" else None)
            assert resp.status_code == code, (method, route_path, headers, resp.status_code)
        checked += 1
    assert checked >= 9                                   # 6 GET + 3 POST
    assert repo.list_tasks() == []                        # the rejected requests changed nothing


def test_unconfigured_token_makes_every_non_healthz_endpoint_fail_closed(repo):
    app = api.create_app(repo, {})
    c = TestClient(app)
    for method, route_path in _endpoints(app):
        want = 204 if route_path == "/healthz" else 503
        assert c.request(method, _concrete(route_path), json={} if method == "POST" else None).status_code == want, route_path


def test_healthz_is_public_and_returns_no_data(repo):
    for env in ({api.TOKEN_ENV: TOKEN}, {}):
        c = TestClient(api.create_app(repo, env))
        r = c.get("/healthz")
        assert r.status_code == 204 and r.content == b""
        assert c.post("/healthz").status_code == 405           # GET only
