# ADR-0005: AI Scientist v3 isolation boundary

- Status: Proposed (amended 2026-11-07 after the M3 review)
- Date: 2026-11-06

## Context
AI Scientist v3 is, by assumption, a third-party autonomous research agent that plans experiments, writes and executes code, and may want broad network and compute access (flagged as open question Q7: source, license and exact behaviour must be reviewed before M5). Its output is high-volume, unreviewed, and may be wrong or adversarial. The Executor pipeline processes trusted-ish, contract-bound work with tight network policy. A breach or contamination in one must not propagate to the other or to the control plane.

## Decision
The boundary is defined by **five separations**, each with a negative test (TSEC-05 and M5 host tests):

| Dimension | Executor | AI Scientist v3 |
|---|---|---|
| Container/compose project | `airos-exec` | `airos-sci` (separate project file) |
| Network | `exec-net` (internal) + allowlist proxy to Anthropic host | `sci-net` (internal) + its **own** allowlist proxy to its own LLM host(s) only |
| Credentials | Executor key (proxy-injected preferred) | Its own key, own provider project/spend cap; never the Executor's, Director's or Planner's |
| Volumes | `data/workspaces/<run>/…` | `sci-in` (ro in container), `sci-work` (private rw), `sci-out` (rw; host mounts ro) — named volumes unique to Scientist |
| Budget | `task/root/global` | additionally its own `ai_scientist` budget; reserved before start |

Further rules:
1. **One-way file protocol:** control plane writes `request.json` + declared inputs to `sci-in`; container writes results to `sci-out`; control plane ingests via the standard Verifier path. No sockets, no shared IPC, no Docker socket, no `--network container:`.
2. Outputs are class `scientist_output`, `trust_level=quarantined`; only a human `artifacts release` moves them to `untrusted`; they are never inputs to the Executor until `reviewed` and then only by explicit `input_artifact_refs`.
3. No inputs of class `work_product` or `external_handoff` unless named in the contract and approved (`external_handoff`-style data egress approval applies because the Scientist is a separate trust domain).
4. Runs are never auto-retried; cost and side effects are large (PROPOSAL §8.5).
5. Same container hardening template as ADR-0006; CPU-only; GPU is out of scope (NG2).
6. Image pinned by digest; upstream code pinned to a reviewed commit; the Scientist image is built separately and shares no Dockerfile stage with the Executor image.

## Consequences
+ Compromise or runaway behaviour in the Scientist cannot read Executor workspaces, keys, or the control-plane DB, and cannot reach the Executor network.
+ Cost isolation: a runaway Scientist cannot drain Executor budget.
− Duplicate infrastructure (second proxy, second image, second budget) and more operational work.
− File-drop protocol is slow and coarse; no interactive feedback loops.
− Isolation only as strong as Docker (ADR-0006); a kernel/container escape is not covered — hence optional gVisor/Kata and recommendation to run on a dedicated VM.
− Humans become a bottleneck at quarantine release (intentional).

## Alternatives
- **Run Scientist as just another Executor task:** simplest, but shares network policy, credentials and workspace conventions — rejected as the explicit requirement is full isolation.
- **Separate VM/host:** stronger; adopt later if M5 review finds the Scientist needs more privileges. Orthogonal to this decision.
- **Shared volume with ACLs:** subtle misconfiguration risk; rejected in favour of one-way copies.
- **Run the Scientist outside our system entirely and import results manually:** safest and a valid fallback (the manual import path exists); loses orchestration and budget tracking.

## Amendment (M3 review, 2026-11-07)

### AI Scientist v3 is untrusted, full-autonomy code
Its upstream README launches agents with **full tool permissions** (it writes and executes code, spawns processes, and chooses its own
commands). We therefore treat it exactly like hostile code that happens to be useful. It may only ever run under all of these, none optional:

1. **Network:** an allowlist of exactly **one provider host**, default deny, denials audited (egress policy as data; see `config/egress.toml`).
2. **No host mounts.** One scoped writable workspace volume; nothing else. The host `docker.sock` is **never** mounted into any agent container.
3. **Own credentials only** (one dedicated subscription token or login volume that no other adapter holds; never shared env files).
4. **Hard caps:** wall-clock limit enforced by the control plane, and a credit allocation (`ai_scientist` = 0 until a human raises it, with approval
   bound to the contract hash).
5. **Token-revocation runbook** (below) exercised before first use.
6. **Final-evaluation data is not mountable** into its container (mount validator denylist + separate OS user/volume).
7. The adapter remains a **fail-closed stub** until M5; nothing in this repository can start it.

### Token-revocation runbook (any adapter credential)
1. Stop the control plane (or `enabled=false` for the role in `config/agents.toml`); the role then fails closed at routing.
2. Revoke the credential at the provider (account page: sign out / revoke the long-lived token or the logged-in device/session).
3. Remove the host environment variable / delete the dedicated login volume; `docker volume rm` the role's volume.
4. Search audit rows, artifacts and logs for the old token fragment prefix (redaction should have removed it; a hit is an incident).
5. Re-issue a NEW credential, update only that role's variable, re-run the human attestation (`usage_credits_attested_off`) and the live check.

### Credential isolation between adapters
Each adapter has **its own credential** and **never shares a container, volume or env file with another adapter**. The run-spec generator refuses
a spec that would mount another role's login volume or inject another role's token variable (tested).

### M5 pre-flight (not done now)
Before AI Scientist is enabled: (a) read and record **how agsci launches Docker** (client library, flags, privileged/socket use) in this ADR;
(b) confirm no code path requires the host `docker.sock` inside the agent container, and refuse any that does; (c) evaluate running the
Scientist in a **dedicated rootless-Docker VM** separate from the control-plane host.
