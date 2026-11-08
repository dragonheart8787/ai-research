# ADR-0001: SQLite now, with a PostgreSQL migration path

- Status: Proposed
- Date: 2026-11-06

## Context
The control plane is a single process on one host, with low write volume (tens of state transitions per minute), but it needs transactions, constraints, partial unique indexes, and triggers to make duplicate prevention and append-only guarantees enforceable by the database rather than by convention. We want zero operational overhead now and an upgrade path when concurrency or multi-host needs appear. The environment has SQLite 3.53.1 and no database server.

## Decision
1. Use **SQLite** in WAL mode with `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`, one writer (the control plane, enforced by an `flock`).
2. Access the database only through a **repository layer**: one `Protocol` per aggregate (tasks, runs, approvals, budgets/ledger, artifacts, events), plain parameterised SQL, **no ORM**. Services depend on the Protocols, never on `sqlite3`.
3. Constrain SQL to a portable subset (PROPOSAL §5.0): UUIDv7 text IDs, epoch-millisecond integers, integer micro-USD, JSON as validated text, partial unique indexes, CAS updates for leases/state.
4. Migrations are numbered `.sql` files with optional `pg/` overrides; a migration runner records applied versions and checksums.
5. Write a **dual-backend contract test suite** that runs the same repository tests against any backend; PostgreSQL support is added when the trigger conditions below fire (not before).

Trigger to migrate: sustained `SQLITE_BUSY` rate > 1 % of write transactions, or a second control-plane host/process is required, or database > 10 GiB, or a dedicated queue (ADR-0002) needs `SKIP LOCKED`.

## Consequences
+ Zero infrastructure; whole state is one file plus the CAS directory; trivial backup (`VACUUM INTO`/`.backup`).
+ DB-level enforcement of the invariants (unique keys, transition trigger, append-only triggers).
− Single writer: long transactions block heartbeats. Rule: transactions never span an LLM/container call; budget < 50 ms each.
− Portable-subset discipline costs some convenience (no SQLite-specific `INSERT OR REPLACE`, no `rowid` tricks).
− Triggers must be re-expressed for PostgreSQL (PL/pgSQL) and verified by the same contract tests.
− No multi-host; a standby requires Litestream-style replication or the PostgreSQL migration.

## Alternatives
- **PostgreSQL from day one:** best concurrency and `SKIP LOCKED`, but requires a server (container or install) that this environment lacks and that violates "no containers/installs" for now; adds ops burden before there is any load.
- **SQLAlchemy ORM:** eases dialect switching but hides SQL, makes triggers/partial indexes second-class, and creates lock-in the user explicitly wants to avoid.
- **SQLAlchemy Core / query builder:** viable later; rejected now to keep SQL reviewable and the dependency surface small.
- **Flat files / JSONL:** no constraints or transactions; cannot enforce uniqueness or atomic state+event writes.
