# ADR-0004: GitHub as the artifact handoff for ChatGPT Work

- Status: Proposed
- Date: 2026-11-06

## Context
The "ChatGPT Work Researcher" role must be supported, but we **cannot assume a callable API** for it. What we do know it can plausibly do is run scheduled or event-triggered workflows and read/write GitHub. We need a handoff that is asynchronous, idempotent, auditable, works with a manual fallback, and does not give the other party credentials into our system.

## Decision
1. Use a **dedicated private GitHub repository** (default `ai-research-handoff`; open question Q3) as the exchange medium. Requests go to `requests/<task_id>/{request.md, contract.json, manifest.json, inputs/…}`; results come back at `results/<task_id>/{response.json, outputs/…}` on branch `results/<task_id>` (commit or PR).
2. The control plane holds a GitHub token scoped to that repo only (write `requests/**`, read `results/**`). The ChatGPT Work side gets no credentials to our host or main repo.
3. Dispatch requires a human `external_handoff` approval (data leaves the host). Dispatch content/path is deterministic so repeats are no-ops.
4. Return path = `AgentResponse` v1 JSON + files; ingest verifies path, schema, per-file sha256, size, author allowlist (and signature if available). Failure → `handoff_invalid`, never auto-retried.
5. Ingested artifacts are `trust_level=untrusted` and are data only; human review is mandatory before `completed` (T23).
6. The run waits in `awaiting_external` with a 72 h `external_deadline`; polling is read-only, ≤ 12 requests/hour; timeouts → `timed_out`.
7. **Manual handoff** is first-class: `airos artifacts import --task <id> …` records the human as producer-of-record.
8. Workflow triggers (schedule / push / label / manual) are the other side's concern; our protocol depends only on repository state.
9. No self-hosted GPU runner or privileged Action is attached to the handoff or main repo; Actions in them use GitHub-hosted runners and `pull_request` semantics only.

## Consequences
+ Works with any producer that can write files to GitHub, including a human; fully asynchronous; every exchange is also in git history (a second, independent record).
+ No inbound network path into our host; no credentials shared with the external service.
− Latency (minutes to days); polling load and GitHub rate limits; dependent on GitHub availability.
− Content is exposed to GitHub (private repo) and to ChatGPT Work — hence the approval gate and the rule that restricted classes never go out.
− Results are untrusted text → prompt-injection surface; mitigated by trust levels and human review.
− Author-spoofing risk if the repo is shared; mitigated by allowlist, signatures, branch protection.

## Alternatives
- **Assume an HTTP API for ChatGPT Work:** unverified; would couple us to an interface that may not exist. Revisit via a new adapter if one is documented.
- **Email/Drive/Slack handoff:** weaker structure and audit, more credentials.
- **Same repository, branch-based:** fewer repos but mixes untrusted writers with our source and CI secrets; rejected by default.
- **Shared filesystem/S3 bucket:** adds cloud credentials and does not leverage ChatGPT Work's GitHub integration.
- **Manual copy/paste only:** retained as fallback, but not scalable or auditable by itself.

## Amendment (M3 fixes, A9): author identity is forgeable; signatures are an optional switch
Git author name/e-mail are plain metadata that anyone who can push can set to any value, so the author allowlist is a **consistency check, not authentication**.
The real defense is **repository access control** (who can push to the exchange repository, branch protection, 2FA on that account).
`require_signature` (`config/agents.toml`, `[roles.chatgpt_work]`) is a switch, **default false**. With it off, an unsigned result is accepted only from an
allowlisted author **and** always needs human review (`human_review: mandatory`); with it on, an unsigned or unverified result is rejected. A BAD signature
is rejected in both modes. Handoff output is always untrusted data.
