"""Minimal FastAPI app.

* Mutations (create root task, decide approval, cancel) require the OPERATOR TOKEN, read once from the environment
  variable AIROS_OPERATOR_TOKEN. They act as the registered human principal `operator`. If the variable is unset or empty
  every mutation fails closed with 503. The token is compared in constant time and is never stored, logged or returned.
* Status/dashboard endpoints are GET-only and read-only.
* Agents never receive the token: build the AgentRegistry from `agent_environ(os.environ)`, which strips it.
* serve() binds 127.0.0.1 by default and refuses any other host unless explicitly allowed.
"""

import hashlib
import hmac
import html
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from airos.domain.schemas import TaskContract
from airos.domain.states import Actor, ActorKind, Event
from airos.errors import (
    AirosError,
    ApprovalExpired,
    GuardFailed,
    IdempotencyConflict,
    NotAuthorized,
    NotFound,
    StaleApproval,
    VersionConflict,
)
from airos.persistence.sqlite_repo import SqliteRepository

TOKEN_ENV = "AIROS_OPERATOR_TOKEN"
OPERATOR = Actor(ActorKind.HUMAN, "operator")
DEFAULT_HOST = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def agent_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment handed to agent adapters/containers: everything EXCEPT the operator token."""
    return {k: v for k, v in environ.items() if k != TOKEN_ENV}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: bool
    reason: str | None = None


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    reason: str | None = None


def _status_for(exc: AirosError) -> int:
    if isinstance(exc, NotAuthorized):
        return 403
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, (VersionConflict, IdempotencyConflict, StaleApproval, ApprovalExpired, GuardFailed)):
        return 409
    return 400


def create_app(repo: SqliteRepository, environ: Mapping[str, str]) -> FastAPI:
    token = environ.get(TOKEN_ENV, "")
    token_digest = hashlib.sha256(token.encode()).digest() if token else None
    repo.register_principal(OPERATOR.id, "human", None, "operator (API token holder)")
    app = FastAPI(title="airos control plane", docs_url=None, redoc_url=None, openapi_url=None)

    def require_operator(authorization: str | None = Header(default=None)) -> Actor:
        if token_digest is None:
            raise HTTPException(503, "operator token is not configured; mutations are disabled")
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise HTTPException(401, "operator token required", headers={"WWW-Authenticate": "Bearer"})
        if not hmac.compare_digest(hashlib.sha256(presented.encode()).digest(), token_digest):
            raise HTTPException(403, "invalid operator token")
        return OPERATOR

    def guarded(fn: Any) -> Any:
        try:
            return fn()
        except AirosError as exc:
            raise HTTPException(_status_for(exc), str(exc)) from exc

    # ---------------------------------------------------------------- read-only
    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        head = repo.head()
        v = repo.verify_chain_with_anchors()
        counts: dict[str, int] = {}
        for t in repo.list_tasks():
            counts[t.state.value] = counts.get(t.state.value, 0) + 1
        return {"tasks_by_state": counts, "audit_head_seq": head.seq, "audit_head_hash": head.entry_hash,
                "audit_chain_ok": v.ok, "anchors_checked": v.anchors_checked, "audit_problems": v.problems}

    @app.get("/v1/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return [{"task_id": t.task_id, "state": t.state.value, "role": t.assigned_role.value, "version": t.version,
                 "kind": t.kind.value, "parent_task_id": t.parent_task_id} for t in repo.list_tasks()]

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        t = guarded(lambda: repo.get_task(task_id))
        return {**t.model_dump(mode="json"), "approvals": [a.model_dump(mode="json") for a in repo.list_approvals(task_id)],
                "runs": [r.model_dump(mode="json") for r in repo.list_runs(task_id)]}

    @app.get("/v1/audit/verify")
    def audit_verify() -> dict[str, Any]:
        v = repo.verify_chain_with_anchors()
        return {"ok": v.ok, "checked": v.chain.checked, "broken_seq": v.chain.broken_seq, "problems": v.problems,
                "head_seq": v.chain.head_seq, "head_hash": v.chain.head_hash}

    @app.get("/v1/budgets/{budget_id}")
    def budget(budget_id: str) -> dict[str, Any]:
        s = guarded(lambda: repo.budget_status(budget_id))
        return {"budget_id": s.budget_id, "limit_usd_micro": s.limit_usd_micro, "used_usd_micro": s.used_usd_micro,
                "limit_tokens": s.limit_tokens, "used_tokens": s.used_tokens, "state": s.state}

    @app.get("/")
    def dashboard() -> Any:
        from fastapi.responses import HTMLResponse

        rows = "".join(f"<tr><td>{html.escape(t.task_id)}</td><td>{html.escape(t.state.value)}</td>"
                       f"<td>{html.escape(t.assigned_role.value)}</td></tr>" for t in repo.list_tasks())
        v = repo.verify_chain_with_anchors()
        return HTMLResponse(f"<h1>airos (read-only)</h1><p>audit chain ok: {v.ok}</p>"
                            f"<table><tr><th>task</th><th>state</th><th>role</th></tr>{rows}</table>")

    # ---------------------------------------------------------------- operator-token mutations
    @app.post("/v1/tasks", status_code=201)
    def create_root_task(contract: TaskContract, actor: Annotated[Actor, Depends(require_operator)]) -> dict[str, Any]:
        res = guarded(lambda: repo.create_task(contract, actor))
        return {"task_id": res.task.task_id, "created": res.created, "version": res.task.version}

    @app.post("/v1/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: Decision, actor: Annotated[Actor, Depends(require_operator)]) -> dict[str, Any]:
        ap = guarded(lambda: repo.decide_approval(approval_id, body.grant, actor, body.reason))
        return {"approval_id": ap.approval_id, "status": ap.status.value}

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str, body: CancelRequest, actor: Annotated[Actor, Depends(require_operator)]) -> dict[str, Any]:
        t = guarded(lambda: repo.transition(task_id, Event.CANCEL, actor, body.expected_version, reason=body.reason))
        return {"task_id": t.task_id, "state": t.state.value, "version": t.version}

    return app


def serve(app: FastAPI, host: str = DEFAULT_HOST, port: int = 8765, *, allow_non_loopback: bool = False) -> None:
    """Run the API. Binds loopback only unless explicitly overridden."""
    if host not in LOOPBACK_HOSTS and not allow_non_loopback:
        raise ValueError(f"refusing to bind {host!r}: the API listens on loopback only unless allow_non_loopback=True")
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
