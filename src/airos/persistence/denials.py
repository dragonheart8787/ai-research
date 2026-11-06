"""Audit of authorization denials, written through a SEPARATE connection and transaction.

The main transaction has already rolled back when this runs, so the denial is not lost with it. The log is protected
against floods: per (principal-bucket, action) at most ONE audit event per window; at most MAX_EVENTS_PER_WINDOW
events per window overall; everything else only increments counters in two bounded tables. Unregistered principals
share one bucket, so an attacker cannot grow the tables by inventing identities.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from airos.domain.states import Actor

if TYPE_CHECKING:
    from airos.persistence.sqlite_repo import SqliteRepository

WINDOW_MS = 60_000
MAX_EVENTS_PER_WINDOW = 50
UNREGISTERED = "<unregistered>"


class DenialAuditor:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo = repo

    def record(self, actor: Actor, action: str, task_id: str | None, detail: str) -> bool:
        """Returns True if an audit event was written, False if the denial was only counted."""
        repo = self._repo
        conn = repo._denial_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = repo.clock.now_ms()
            window = now // WINDOW_MS
            known = conn.execute("SELECT 1 FROM principals WHERE principal_id=?", (actor.id,)).fetchone() is not None
            key = f"{actor.id if known else UNREGISTERED}|{action}"
            st = conn.execute("SELECT window_id, count, total FROM authz_denial_state WHERE key=?", (key,)).fetchone()
            conn.execute("DELETE FROM authz_denial_global WHERE window_id < ?", (window,))
            conn.execute("INSERT OR IGNORE INTO authz_denial_global(window_id, events, suppressed) VALUES(?,0,0)", (window,))
            g = conn.execute("SELECT events FROM authz_denial_global WHERE window_id=?", (window,)).fetchone()
            carried = 0
            emit = False
            if st is not None and st["window_id"] == window:
                conn.execute("UPDATE authz_denial_state SET count=count+1, total=total+1 WHERE key=?", (key,))
            else:
                carried = max(st["count"] - 1, 0) if st is not None else 0  # suppressed in that key's previous window
                emit = g["events"] < MAX_EVENTS_PER_WINDOW
                total = (st["total"] if st is not None else 0) + 1
                conn.execute("INSERT OR REPLACE INTO authz_denial_state(key, window_id, count, total) VALUES(?,?,1,?)",
                             (key, window, total))
            if emit:
                conn.execute("UPDATE authz_denial_global SET events=events+1 WHERE window_id=?", (window,))
                payload: dict[str, Any] = {"action": action, "detail": detail, "bucket": key.split("|")[0],
                                           "suppressed_in_previous_window": carried}
                repo._append_event(conn, "authz.denied", actor, task_id=task_id, payload=payload)
            else:
                conn.execute("UPDATE authz_denial_global SET suppressed=suppressed+1 WHERE window_id=?", (window,))
            conn.execute("COMMIT")
            return emit
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
