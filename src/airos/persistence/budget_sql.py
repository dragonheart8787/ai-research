"""Budget ledger logic. Every function runs inside the caller's BEGIN IMMEDIATE transaction.

Ledger arithmetic (per budget_id):
  used  = SUM(usd_micro/tokens) over kinds reserve, release, settle, adjust      (reserve +R, release -R, settle +A)
  limit = base (budgets row, or the task contract for `task:<id>`) + SUM(grant)
A reservation is one `reserve` row per level (task, root, global_daily, and role for real adapters). It is OPEN until a
`release` row references it (reserve_ref). Settle = release(-R) + settle(+actual); abandoned runs settle at worst case (+R).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from airos.domain.limits import Limits
from airos.domain.schemas import TaskRecord


def task_budget_id(task_id: str) -> str:
    return f"task:{task_id}"


def root_budget_id(root_task_id: str) -> str:
    return f"root:{root_task_id}"


def role_budget_id(role: str) -> str:
    return f"role:{role}"


def global_budget_id(now_ms: int) -> str:
    return "global:" + datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class ReserveOutcome:
    ok: bool
    reason: str | None = None
    already_held: bool = False
    reserved_usd_micro: int = 0
    reserved_tokens: int = 0
    levels: list[str] = field(default_factory=list)


def task_limits(task: TaskRecord, grant_usd: int, grant_tokens: int) -> tuple[int, int]:
    """Per-task limit: the contract's per-attempt caps times every permitted attempt (1 + max_retries), plus grants.

    Each attempt RESERVES at most one attempt's cap (worst case), so one crashed attempt (settled at worst case)
    cannot by itself make later permitted attempts impossible.
    """
    rl = task.contract.resource_limits
    attempts = 1 + rl.max_retries
    return rl.max_usd_micro * attempts + grant_usd, rl.max_tokens * attempts + grant_tokens


def usage(conn: sqlite3.Connection, budget_id: str) -> tuple[int, int]:
    r = conn.execute(
        "SELECT COALESCE(SUM(usd_micro),0), COALESCE(SUM(tokens),0) FROM budget_ledger "
        "WHERE budget_id=? AND kind IN ('reserve','release','settle','adjust')", (budget_id,)).fetchone()
    return int(r[0]), int(r[1])


def grants(conn: sqlite3.Connection, budget_id: str) -> tuple[int, int]:
    r = conn.execute(
        "SELECT COALESCE(SUM(usd_micro),0), COALESCE(SUM(tokens),0) FROM budget_ledger WHERE budget_id=? AND kind='grant'",
        (budget_id,)).fetchone()
    return int(r[0]), int(r[1])


def ensure_budget(conn: sqlite3.Connection, budget_id: str, scope: str, ref: str, usd: int, tokens: int,
                  created_by: str, now_ms: int) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO budgets(budget_id,scope,scope_ref,limit_usd_micro,limit_tokens,state,created_by,created_at_ms)"
        " VALUES(?,?,?,?,?,'active',?,?)", (budget_id, scope, ref, usd, tokens, created_by, now_ms))
    row: sqlite3.Row = conn.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
    return row


def effective_limits(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[int, int]:
    gu, gt = grants(conn, row["budget_id"])
    return row["limit_usd_micro"] + gu, row["limit_tokens"] + gt


def open_task_reserve(conn: sqlite3.Connection, task_id: str, attempt: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT r.* FROM budget_ledger r WHERE r.task_id=? AND r.attempt=? AND r.kind='reserve' AND r.budget_id=? "
        "AND NOT EXISTS (SELECT 1 FROM budget_ledger x WHERE x.kind='release' AND x.reserve_ref=r.entry_id)",
        (task_id, attempt, task_budget_id(task_id))).fetchone()
    return row


def open_reserve_rows(conn: sqlite3.Connection, task_id: str, attempt: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT r.* FROM budget_ledger r WHERE r.task_id=? AND r.attempt=? AND r.kind='reserve' "
        "AND NOT EXISTS (SELECT 1 FROM budget_ledger x WHERE x.kind='release' AND x.reserve_ref=r.entry_id) "
        "ORDER BY r.entry_id", (task_id, attempt)))


def _insert(conn: sqlite3.Connection, *, entry_id: str, budget_id: str, task_id: str, run_id: str | None, kind: str,
            usd: int, tokens: int, attempt: int, reserve_ref: str | None, key: str, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO budget_ledger(entry_id,budget_id,task_id,run_id,kind,usd_micro,tokens,wall_s,approval_id,"
        "idempotency_key,created_at_ms,attempt,reserve_ref) VALUES(?,?,?,?,?,?,?,0,NULL,?,?,?,?)",
        (entry_id, budget_id, task_id, run_id, kind, usd, tokens, key, now_ms, attempt, reserve_ref))


def reserve(conn: sqlite3.Connection, task: TaskRecord, limits: Limits, now_ms: int, real_role: bool,
            new_id: Callable[[], str]) -> ReserveOutcome:
    """Reserve the worst-case spend for the task's NEXT attempt at every level, all-or-nothing."""
    attempt = task.attempt_count + 1
    if open_task_reserve(conn, task.task_id, attempt) is not None:
        return ReserveOutcome(True, already_held=True)
    tb = task_budget_id(task.task_id)
    used_u, used_t = usage(conn, tb)
    gu, gt = grants(conn, tb)
    lim_u, lim_t = task_limits(task, gu, gt)
    rl = task.contract.resource_limits
    r_u, r_t = min(rl.max_usd_micro, max(lim_u - used_u, 0)), min(rl.max_tokens, max(lim_t - used_t, 0))
    if r_t == 0 or (lim_u > 0 and r_u == 0):
        return ReserveOutcome(False, f"per-task budget exhausted ({tb})")
    rows = [
        ensure_budget(conn, root_budget_id(task.root_task_id), "root", task.root_task_id,
                      limits.default_root_usd_micro, limits.default_root_tokens, "system", now_ms),
        ensure_budget(conn, global_budget_id(now_ms), "global_daily", global_budget_id(now_ms)[7:],
                      limits.default_global_daily_usd_micro, limits.default_global_daily_tokens, "system", now_ms),
    ]
    if real_role:  # real adapters default to $0 until a human grants budget
        rows.append(ensure_budget(conn, role_budget_id(task.assigned_role.value), "role", task.assigned_role.value,
                                  limits.real_adapter_default_usd_micro, limits.real_adapter_default_tokens, "system", now_ms))
    for row in rows:
        if row["state"] != "active":
            return ReserveOutcome(False, f"budget {row['budget_id']} is {row['state']}")
        eff_u, eff_t = effective_limits(conn, row)
        u, t = usage(conn, row["budget_id"])
        if eff_u - u < r_u or eff_t - t < r_t:
            return ReserveOutcome(False, f"insufficient budget at {row['budget_id']}")
    gen = conn.execute("SELECT COUNT(*) FROM budget_ledger WHERE task_id=? AND attempt=? AND kind='reserve' AND budget_id=?",
                       (task.task_id, attempt, tb)).fetchone()[0] + 1
    levels = [tb, *(r["budget_id"] for r in rows)]
    for bid in levels:
        _insert(conn, entry_id=new_id(), budget_id=bid, task_id=task.task_id, run_id=None, kind="reserve", usd=r_u,
                tokens=r_t, attempt=attempt, reserve_ref=None, key=f"reserve:{task.task_id}:{attempt}:{gen}:{bid}", now_ms=now_ms)
    return ReserveOutcome(True, reserved_usd_micro=r_u, reserved_tokens=r_t, levels=levels)


def close(conn: sqlite3.Connection, task: TaskRecord, attempt: int, now_ms: int, new_id: Callable[[], str], *,
          run_id: str | None, settle: bool, actual_usd: int | None, actual_tokens: int | None) -> int:
    """Release every open reserve row for (task, attempt). If `settle`, also book the spend.

    actual_* None => WORST CASE (the full reservation is charged): used for abandoned/cancelled runs and for any run
    whose usage was not measured. Idempotent: rows already released are skipped.
    """
    rows = open_reserve_rows(conn, task.task_id, attempt)
    for r in rows:
        rid = r["entry_id"]
        _insert(conn, entry_id=new_id(), budget_id=r["budget_id"], task_id=task.task_id, run_id=run_id, kind="release",
                usd=-r["usd_micro"], tokens=-r["tokens"], attempt=attempt, reserve_ref=rid, key=f"release:{rid}", now_ms=now_ms)
        if settle:
            a_u = r["usd_micro"] if actual_usd is None else actual_usd
            a_t = r["tokens"] if actual_tokens is None else actual_tokens
            _insert(conn, entry_id=new_id(), budget_id=r["budget_id"], task_id=task.task_id, run_id=run_id, kind="settle",
                    usd=a_u, tokens=a_t, attempt=attempt, reserve_ref=rid, key=f"settle:{rid}", now_ms=now_ms)
        brow = conn.execute("SELECT * FROM budgets WHERE budget_id=?", (r["budget_id"],)).fetchone()
        if brow is not None:
            eff_u, eff_t = effective_limits(conn, brow)
            u, t = usage(conn, brow["budget_id"])
            if u > eff_u or t > eff_t:
                conn.execute("UPDATE budgets SET state='exhausted' WHERE budget_id=?", (brow["budget_id"],))
    return len(rows)


def reactivate_if_within_limit(conn: sqlite3.Connection, budget_id: str) -> None:
    brow = conn.execute("SELECT * FROM budgets WHERE budget_id=? AND state='exhausted'", (budget_id,)).fetchone()
    if brow is not None:
        eff_u, eff_t = effective_limits(conn, brow)
        u, t = usage(conn, budget_id)
        if u <= eff_u and t <= eff_t:
            conn.execute("UPDATE budgets SET state='active' WHERE budget_id=?", (budget_id,))
