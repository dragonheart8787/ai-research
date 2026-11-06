# ADR-0009: Approvals bind to task version and contract hash

- Status: Accepted (human review 2026-11-06, "approved with changes")
- Date: 2026-11-06

## Context
An approval is only meaningful for exactly what the human saw. The original proposal bound approvals to `spec_hash` and froze the spec at `planned`. Review requires the binding to include the task's version, and requires that any contract *or budget* change invalidates existing approvals, without forcing a brand-new task for every edit.

## Decision
1. Every task has `version` (integer, starts at 1) and `contract_sha256` (sha256 of the canonical JSON contract, which includes `resource_limits`, i.e. the budget the task may spend, and `allowed_actions`; it excludes `task_id` and mutable state).
2. An approval records `task_id`, `task_version` and `contract_sha256` at request time. These columns are immutable (DB trigger).
3. `version` is bumped on **every** state transition and on **every** amendment (`amend_task`). An amendment recomputes `contract_sha256`.
4. In the same transaction as an amendment, all `pending` and `granted` approvals of the task become `invalidated` and an audit event records old/new hash and count. If the task was `approved`, it returns to `awaiting_approval` (event `REAPPROVE`, actor `system`).
5. An approval is **valid** iff `status='granted'` and `task_version == task.version` and `contract_sha256 == task.contract_sha256`.

   Because state transitions also bump `version`, a *granted* approval would be stale after any later transition. Therefore validity is evaluated against the version **as of the last amendment**: the repository stores `contract_version` (bumped only by amendments) next to `version`, and approvals bind `task_version := contract_version`. (`version` remains the CAS counter.)
6. Guards `approvals_valid` (APPROVE, QUEUE-time check, UNBLOCK) and `decide_approval` use this rule; a decision on a stale approval is refused and the approval is marked `invalidated`.
7. Budget increases that change a task's `resource_limits` are amendments (rule 3). Increases to shared budgets (ledger grants) follow the separate human-approval path and do not alter a task's contract.

## Consequences
+ A human approval can never be replayed against a modified contract or raised budget.
+ Amendment is cheap (no new task), while remaining fully audited.
− Every amendment forces re-approval; for approved tasks this adds latency. Intentional.
− Two counters (`version` for CAS, `contract_version` for approval binding) must be kept distinct; covered by tests.

## Alternatives
- **Freeze at `planned`; change = new task (`supersedes_task_id`)**: simple and safe, but heavy for typo-level edits and loses task identity; rejected by review.
- **Bind only to hash**: detects content change but not "same content, re-planned context"; the version also gives ordering for audit. Kept both.
- **Bind to `version` as the CAS counter**: every transition would invalidate approvals; unusable.
