# ADR-0012: Scope deviation - real adapters appeared in M3

- Status: Accepted (human directive)
- Date: 2026-11-07

## Context
PROPOSAL §14 defines M3 as artifacts / verifier / dashboard / local-fake-repo handoff, puts the real Executor in M4 and the real Director/Planner and
GitHub handoff in M5. At the M2 review the human asked for the real Director, Planner, Executor and handoff adapters in M3, fixture-tested only.

## Decision
Real adapters were built in M3 behind a seam (first an HTTP transport, now a CLI-runner seam after ADR-0011), fixture-tested, and **disabled by
default**. Nothing live is exercised before the M4b VM live check. The PROPOSAL M3 items that fit the rules were also delivered (lineage,
`verify_all`, local handoff); the host-subprocess check runner was deferred to the sandbox milestone.

## Consequences
+ Integration risk is found early, offline. - The "verified" claim for these adapters is limited to fixtures; ADR-0010/M3-NOTES/M4A-NOTES say so.
