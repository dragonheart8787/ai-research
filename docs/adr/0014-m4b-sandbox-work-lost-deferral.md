# ADR-0014: M4b sandbox work lost; deferral

- Status: Accepted
- Date: 2026-11-08

## Context
An M4b Part 1 implementation (about a dozen commits, roughly 1100 tests, run against real rootless Docker on a developer's WSL2 machine) was never pushed.
Its environment was permanently deleted, and with it the code, tests and logs. What survives is a set of findings and design decisions reported during that
session, recorded in `docs/architecture/M4B-DESIGN-RECORD.md` and labelled unverified and not reproducible. The credential-revocation finding (no confirmed way
to revoke a minted subscription token) also leaves the live contract check and M5 without an acceptable credential path.

## Decision
1. The lost work is **not** redone now. Container isolation, egress enforcement, the container check runner and hostile-workload behaviour are recorded as
   "not verified in this repository (historical results only)". Real CLI behaviour stays "not verified".
2. The live contract check (Part 2) stays deferred indefinitely and is never run; adapters stay disabled; M5 stays blocked.
3. **Conditions to resume:**
   - a revocable credential path exists, or the authentication decision (ADR-0011) is changed by the human;
   - a Docker-capable environment is available (a rootless daemon, ideally a dedicated VM rather than WSL2);
   - every commit is pushed as it is made;
   - a backup export of the working environment is taken at agreed points.
4. The design record is a starting point for M4b, not a specification: each item must be re-derived and tested here before it is relied on.

## Lessons
- Never keep unpushed work only inside a disposable environment. Push after every commit.
- Back up (export) an environment before anything that could delete it.
- Record findings in the repository while they are fresh, even as a draft.

## Consequences
+ Reported findings are preserved without being mistaken for verified status.
- The M4a spec-only state remains the latest verified state; M4b effort is repeated when conditions are met.
