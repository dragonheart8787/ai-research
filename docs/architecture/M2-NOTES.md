# M2 (governance) implementation notes, deviations and known gaps

Scope delivered on `claude/m2-governance` (mock adapters only; no network, containers or host shell).
PROPOSAL §14 defines M2 as "services (approvals, budgets, authz), scheduler/worker with leases + recovery, all five mock
adapters, minimal API". (The task text pointed at "section 15", which is the risks/open-questions section; §14 was used.)

## Deviations from PROPOSAL (and why)

| # | PROPOSAL | M2 | Why |
|---|---|---|---|
| 1 | Director may cancel non-terminal tasks in its root (§7.1 note 6, T02/T05/T12/T16/T22/T29) | **No agent role can cancel.** Cancel is human-only, everywhere | Explicit M2 requirement; matrix and transition table updated; tested |
| 2 | Reservation at T14 (`queued -> running`) | Reservation happens **before** `queued` (T10/T18/T24/T28 and START all carry the `reservation_held` guard; a DB trigger enforces it) | Explicit M2 requirement: no path to queued/running without a ledger row |
| 3 | Per-task budget = task `max_usd` | Per-task limit = per-attempt cap x (1 + max_retries) (+ grants); each attempt reserves at most one cap | Otherwise one crashed attempt (settled at worst case) would make every permitted retry impossible |
| 4 | Role budgets for "real adapters" | `role:<role>` level applies only when the registry marks the role `real`; default 0 USD / 0 tokens | Real adapters cannot spend until a human grants budget |
| 5 | Matrix in `config/permissions.yaml` | `src/airos/security/permissions.json` (data, loaded at import); a test enumerates every (role, action) pair against a hand-written oracle | No YAML dependency; JSON is data |
| 6 | Unused matrix rows (`agent.route`, `audit.verify`) | Removed: they are not enforced by any API (they take no actor) | A matrix row that nothing enforces would be a false claim |
| 7 | AI Scientist output is `quarantined`; ChatGPT Work output is `untrusted` (ADR-0005) | **Decided (human, M2 review): Scientist output STAYS `quarantined`.** It is forced by the registry whatever the caller claims, is unreadable to every agent except the producing scientist until a human releases it, and release goes to `untrusted`, never `trusted` | ADR-0005; the task wording "marked untrusted" is read as "never trusted", with quarantine as the stricter untrusted state |
| 8 | Settlement recomputes USD from tokens x `pricing.toml` (TB-02) | Done in review fix 6 (`airos.pricing`, JSON table) | - |
| 9 | Approval grants expire after 7 days (T10/T11) | Done in review fix 7 | - |
| 10 | `limit_increase` approval for >25 tasks per root | Done in review fix 8 | - |
| 11 | Timeout: wall-clock kill, `max_timeout_retries` | Done in review fix 5 (the process/container *kill* itself is M4) | - |
| 12 | Schema-violating response retried once | Done in review fix 9 | - |
| 13 | Authz denials audited (M1 note 15) | Done: separate connection/transaction, per-(principal bucket x action) dedup per 60 s window, 50 events/window cap, bounded counter tables | M2 requirement |
| 14 | Minimal API | FastAPI app with operator token (`AIROS_OPERATOR_TOKEN`), read-only GETs, loopback-only `serve()`; `fastapi`, `uvicorn`, `httpx` added to the hashed lockfile | PROPOSAL §14 lists it for M2; M1 had forbidden FastAPI only "yet" |

## M2 review fixes (applied after the human review, each with tests)

| # | Fix | Where |
|---|---|---|
| 1 | Scientist output stays `quarantined` (forced regardless of the caller's claim) | `test_artifacts.py` |
| 2 | Every API endpoint requires the operator token except `/healthz` (204, no body); routes are enumerated from what the app serves | `test_api.py` |
| 3 | A failed/timed-out objective is cascaded by the SYSTEM actor (`cascade.cancel` matrix row); events carry `cause_task_id`/`cause_event`; running children lose their lease and settle at worst case; idempotent | `test_objective_cascade.py` |
| 4 | Worst-case total (per-attempt cap x (1 + max_retries)) is derived, hashed into the contract, stored on every approval and returned by the API | `test_worst_case.py` |
| 5 | Hard wall-clock timeout enforced by the repository/worker (`time_out_run`), independent of the adapter; elapsed >= limit is expired; worst-case settle; one timeout retry | `test_wall_clock_timeouts.py` |
| 6 | USD = tokens x pinned price table (version + sha256 stored on the run); adapter USD ignored (cross-checked, >5% divergence flagged); unknown model settles at worst case + `pricing.flag`; real adapters need a price entry | `test_pricing.py` |
| 7 | Approval grants expire after 7 days (valid strictly before decided_at + 7d) | `test_grant_expiry.py` |
| 8 | The (limit+1)th task per root needs a human `limit_increase` (bound to the contract hash); hard cap 100 rejects | `test_root_task_limit.py` |
| 9 | A verifier failure is retried once per task; hash mismatches never | `test_verifier_retry.py` |
| - | Test suite runs with `filterwarnings = error` and ONE exact exemption (Starlette's httpx TestClient notice) | `test_warnings_policy.py` |

Limits of these fixes: the 7-day expiry and the timeouts are enforced by service guards using the injectable clock, not by database
triggers (a trigger cannot see the injected clock). Only the *binding* of approvals (version + hash + human decider) is also DB-enforced.
A result that arrives in the instant between the worker's deadline check and `complete_run` can leave registered artifacts for a run
that is then timed out (the check before ingest makes this window small, not zero).

## Known gaps (not claimed as verified)

* Crash injection is limited to: worker death mid-run, adapter crash, lease expiry. The PROPOSAL's TR-01..08 matrix of crash points
  (e.g. between container stop and DB write) needs real containers and is not covered.
* The denial flood bound is proven for the two tables and the audit log; it does not rate-limit the *work* of rejecting requests.
* No container, isolation or egress property is verified anywhere in M2 (nothing starts a container). Nothing is labelled "spec-only"
  because M2 has no container run-spec tests.
* The API has no TLS or rate limiting; it is loopback-only by construction and by default.
