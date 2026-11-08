# Integration roadmap

Status key: **done (tested offline)**, **spec-only**, **blocked**, **optional**. "Done" never means a real provider or container was involved.

## Where things stand
| Milestone | State |
|---|---|
| M1 domain + persistence; M2 governance + mock flow; M3 real adapters behind synthetic fixtures + git handoff | done (tested offline) |
| Part A (subscription-only auth, credit pool, billing-integrity hold, orphans) | done (tested offline) |
| M4a sandbox as specs, policy and a fake-docker runner | done, **spec-only: isolation not verified** (ADR-0013) |
| M4b real sandbox, live contract check | **blocked** (ADR-0014) |
| M5 AI Scientist v3 isolation | **blocked**; the adapter is a fail-closed stub |

## The blocking condition
M4b and M5 are **blocked on a revocable credential path**. Both would place a long-lived subscription token inside a container (M5: a full-autonomy one), and no
confirmed way to revoke a minted token was found (see `M4B-DESIGN-RECORD.md` section h, itself an unverified report). Resuming needs either a credential path that
can be revoked, or a changed authentication decision by the human (ADR-0011), plus a Docker-capable environment, pushing after every commit, and a backup export
(ADR-0014). The live contract check is deferred indefinitely and never run.

## Default posture
**Real adapters are disabled by default**: every role in `config/agents.toml` has `enabled = false`, the usage-credits attestation is `false`, and the Director run cap
is 0. Mock adapters are the only ones that run unless a human deliberately changes the configuration and satisfies the registry's gates. This repository does not
enable them, and its tests use synthetic fixtures only.

## The manual_handoff alternative
Work that needs a provider without minting a token for us goes through `manual_handoff` (ChatGPT Work): the system writes a request to a local git exchange
repository, a human (or their own workflow, under their own login) produces the result and commits it, and ingest treats it as untrusted data needing human
review. It needs no credential from us, no container and no network from the control plane. It is the only integration path that does not depend on the blocked
items. (Interface details: `INTERFACES.md`.) A similar human-in-the-loop pattern for Claude-driven roles is not implemented.

## Next steps that do not need a credential or a daemon
- Keep the mock-mode path and the docs accurate; extend mock scenarios and tests.
- Optional: a container-backed check runner design, only once a Docker-capable environment exists.

## Optional hardening (not started)
- **Clock-divergence guard** (each heartbeat, compare the wall-clock delta with the monotonic delta; on divergence pause expiry decisions, write a `clock_anomaly`
  audit event, never re-run side-effectful tasks). **Deferred because** it was designed only from a reported, unreproduced WSL2 clock anomaly
  (`M4B-DESIGN-RECORD.md` section a); the cause was never proven, the code was lost with the session, and nothing in this repository runs under such a clock.
  It is also reasonable to implement it together with the lease-TTL rules from that record (TTL >= 2 s and >= 10x heartbeat; heartbeats on the monotonic clock).
- Per-role egress proxy, executor image and container-backed `AgentLoop`: part of the blocked M4b.
