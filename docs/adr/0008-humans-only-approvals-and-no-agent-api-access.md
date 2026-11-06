# ADR-0008: Humans are the only approvers; agents have no API access

- Status: Proposed
- Date: 2026-11-06

## Context
Requirement: humans are the only principal who can grant approvals for costly or destructive operations, and no agent may approve its own output. Agents are LLM-driven and susceptible to prompt injection; any credential or API route they hold is a path to self-escalation.

## Decision
1. Principals are `human`, `system` (deterministic control-plane code) and agent roles. **Agent roles hold no API token and make no HTTP calls to the control plane.** Adapters are invoked by the worker; an agent's whole authority is the `AgentResponse` it returns (or files it writes in its run directory).
2. `approvals.status` may become `granted`/`denied` only with `decided_by` referencing a `principals.type='human'` row — enforced by a DB trigger **and** the service layer. The same holds for budget grants (`approval_id` must reference a human-granted `budget_increase`).
3. Review verdicts: reviewer ≠ producer of the run under review (T23); outputs of director/planner/chatgpt_work/ai_scientist tasks and any `untrusted`/mock-mixed artifacts require a **human** reviewer.
4. System auto-approval (T04) exists only for `side_effect_class=none` and cost ≤ `auto_approve_usd` (default 0), is recorded as `policy:auto`, and is not an approval row.
5. Human tokens are issued only through the local CLI, stored as sha256 hashes, and are rejected for use by any adapter code path (tests TA-03, TA-06).

## Consequences
+ A compromised or injected agent cannot approve, spend, widen permissions or alter the audit log through the API, because it has no API.
+ Small attack surface and simple matrix (PROPOSAL §7).
− Human is a throughput bottleneck (approval fatigue); mitigations: batch approval of a plan's tasks per `spec_hash`, clear cost summaries, TTLs.
− Compromise of the human token or the host equals full compromise; mitigate with short-lived tokens, `127.0.0.1` binding, and later 2FA/SSO (open question Q9).

## Alternatives
- **Agent tokens with role scopes:** more flexible but adds the escalation path this ADR removes.
- **Quorum of agents as approvers ("LLM-as-judge approves cost"):** violates the requirement and is manipulable.
- **Time-delay auto-approve:** silent spending by default; rejected.
