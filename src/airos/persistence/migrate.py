"""Plain versioned SQL migrations: NNNN_name.sql, applied in order, checksum-verified."""
from __future__ import annotations

import sqlite3
from importlib import resources

from airos.canonical import sha256_hex
from airos.errors import SchemaMismatch


def _migration_files() -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for entry in resources.files("airos.persistence").joinpath("migrations").iterdir():
        name = entry.name
        if name.endswith(".sql"):
            out.append((int(name.split("_", 1)[0]), name, entry.read_text(encoding="utf-8")))
    return sorted(out)


def migrate(conn: sqlite3.Connection, now_ms: int) -> list[int]:
    """Apply pending migrations; verify checksums of applied ones. Returns versions applied."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL, applied_at_ms INTEGER NOT NULL)"
        )
        done = {r[0]: r[1] for r in conn.execute("SELECT version, sha256 FROM schema_migrations")}
        applied: list[int] = []
        for version, name, sql in _migration_files():
            digest = sha256_hex(sql)
            if version in done:
                if done[version] != digest:
                    raise SchemaMismatch(f"migration {name} was modified after being applied")
                continue
            # executescript() would COMMIT; split safely on our own statement terminator instead.
            for stmt in _split_statements(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, sha256, applied_at_ms) VALUES (?,?,?,?)",
                (version, name, digest, now_ms),
            )
            applied.append(version)
        conn.execute("COMMIT")
        return applied
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _split_statements(sql: str) -> list[str]:
    """Split on ';' using sqlite3.complete_statement so CREATE TRIGGER ... BEGIN ...; END; stays whole."""
    stmts: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        raise SchemaMismatch("trailing incomplete SQL statement in migration")
    return stmts
