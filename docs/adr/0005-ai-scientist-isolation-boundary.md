# ADR-0005: AI Scientist v3 isolation boundary

- Status: Proposed
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
