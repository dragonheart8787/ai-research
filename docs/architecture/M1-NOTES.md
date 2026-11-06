# M1 implementation notes and deviations from PROPOSAL

Scope: domain + persistence only (no API, no scheduler loop, no adapters, no containers, no network).

| # | PROPOSAL said | M1 does | Why |
|---|---|---|---|
| 1 | Contract carries `task_id`; `spec_hash` | Contract has no `task_id`; `contract_sha256` excludes identity. Task identity/state live on `TaskRecord` | An idempotent re-create generates a new `task_id`; hashing it would make duplicates look like conflicts |
| 2 | One `task_version` | Two counters: `version` (CAS, every change) and `contract_version` (only amendments). Approvals bind to `contract_version` + `contract_sha256` (ADR-0009 rule 5) | If approvals bound to the CAS counter, every transition would invalidate them |
| 3 | `container_cpus: float` | `container_cpu_millis: int` | Canonical JSON forbids floats |
| 4 | UUIDv7 ids | `uuid4().hex` (injectable `id_factory`) | stdlib in 3.11 has no UUIDv7 |
| 5 | Run states `leased` + `running` | Single `running` state | In-process worker has no gap between lease and start |
| 6 | Timeout retries limited to 1 | Timeouts share `max_retries` | `timeout_retries` not tracked in M1 |
| 7 | "Side-effectful" undefined | `external_write` and `destructive` are side-effectful: never auto-retried, recovery -> `blocked(crash_recovery_decision)`. `workspace_write` is re-runnable | Workspaces are per-run; effects escaping the workspace cannot be undone |
| 8 | Spec frozen at `planned` | Amendable in draft/planned/awaiting_approval/approved; every amendment bumps `contract_version` and invalidates approvals | Human review, ADR-0009 |
| 9 | Budget reservation in T14 | `GuardContext.budget_reserved` is supplied by the caller; the budget service is M2. Only the ledger table, entry schema and append API exist | Scope |
| 10 | Total tasks per root excludes cancelled drafts | Counts every task under the root | Simpler and stricter |
| 11 | Backoff with +-20 % jitter | Deterministic exponential backoff, no jitter | Reproducible recovery; jitter belongs to the M2 scheduler |
| 12 | Operator token from env var, 127.0.0.1 binding | Not implemented (no API in M1) | M2 |
| 13 | JCS canonical JSON | JCS-compatible subset (strings, ints, bools, null, arrays, objects; floats rejected) | Sufficient for all stored data; vectors in `tests/vectors/audit_chain.json` |
| 14 | Anchor on approval and root completion | Implemented: anchor line on every approval *decision* (grant and deny) and on ACCEPT of a root task | A denial is also a human decision |
| 15 | Authz denials audited | Denials raise and roll back; they are not written to the audit log in M1 | An audit write in a rolled-back transaction is lost; needs a separate pre-commit path in M2 |
