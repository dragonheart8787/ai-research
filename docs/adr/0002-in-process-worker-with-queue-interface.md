# ADR-0002: In-process worker now, `WorkQueue` interface for a dedicated queue later

- Status: Proposed
- Date: 2026-11-06

## Context
Tasks must be leased, heartbeated, retried, timed out and recovered after restart. At the current scale (≤ 2 concurrent runs) a separate broker (Redis/RabbitMQ/Celery) adds failure modes and operational cost without benefit. But embedding scheduling logic directly in the web process would make a later split expensive.

## Decision
1. The scheduler/worker loop runs **inside the control-plane process**: an asyncio supervisor task plus a bounded thread pool (size = `max_concurrent_runs`, default 2) for blocking adapter calls.
2. All scheduling goes through a `WorkQueue` Protocol with exactly these operations: `claim(worker_id, now) -> RunLease | None`, `heartbeat(run_id, worker_id, now) -> bool`, `complete(run_id, worker_id, outcome) -> None`, `release_expired(now) -> list[RunId]`, `enqueue(task_id, not_before)`. State and leases live **in the database** (CAS on `state_version`; lease columns on `runs`), not in memory.
3. Workers carry a per-process `worker_id`; on startup all leases owned by other `worker_id`s are considered dead and recovered per PROPOSAL §8.3.
4. Guarantee: **at-least-once dispatch, at-most-one active run per task** (DB partial unique index); side-effecting tasks are never auto-retried after unknown outcome.

## Consequences
+ No extra infrastructure; crash recovery logic is the same logic a future external queue would need, so it is tested now.
+ A dedicated queue (PostgreSQL `SKIP LOCKED`, Redis streams, or a broker) can replace the implementation without touching services.
− The web process and scheduler share fate; a runaway adapter call can starve the event loop → mitigated by thread pool and per-call timeouts; if observed, run the scheduler as a second process against the same DB (supported by the interface, not by default because of the single-writer flock — would move with ADR-0001 migration).
− No horizontal scaling until the migration trigger in ADR-0001.

## Alternatives
- **Celery/RQ + Redis:** mature, but introduces a broker, serialization and visibility-timeout semantics that conflict with our explicit lease/audit model; rejected for now.
- **Separate worker process from day one:** cleaner isolation but needs inter-process coordination with a single-writer SQLite; defer.
- **APScheduler/cron-style:** time-based, does not model leases, retries or per-task state.
- **In-memory queue only:** loses queued work on crash; rejected — the DB is the queue.
