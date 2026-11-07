# M3 (integrations) notes, deviations, and what is / is not verified

Scope delivered on `claude/m3-integrations` (after the nine M2 review fixes on `claude/m2-governance`). No network, no containers,
no live credentials; the suite blocks sockets for every test.

## Difference from PROPOSAL §14 (reported, as instructed)

PROPOSAL §14 defines **M3** as "artifacts, verifier, status dashboard": full provenance/lineage, an *isolated check runner (subprocess, no
network)*, a read-only status page, anchors, and a GitHub-handoff adapter against a local fake repo. It places the real Executor in **M4** and the real
Director/Planner + real GitHub handoff in **M5**. The human's M3 list instead asks for the real OpenAI Director, Anthropic Planner, Claude Agent SDK Executor
and the git ChatGPT Work handoff, fixture-only. I followed the human's explicit list and also delivered the PROPOSAL-M3 items that fit the rules:

| PROPOSAL M3 item | Status |
|---|---|
| Full provenance / lineage | Done: `ArtifactService.lineage()`, `verify_all()` (content + provenance + every edge); e2e asserts 100 % recompute |
| GitHub handoff against a local fake repo | Done (local bare git repo; invalid author, bad hash, oversize, malformed, missing output, replay all tested) |
| Anchors | Already in M1/M2 |
| Read-only status page | Exists since M2 (now operator-token protected); not extended |
| **Isolated check runner (subprocess)** | **Not done.** Running machine checks such as `command_exit_zero` means executing commands on the host, which conflicts with "no host shell / no containers". Deferred to the M4 sandbox. The verifier still fails closed on check types it cannot run |

## Other deviations / design notes

* The real Executor is written against an `AgentLoop` interface. The Claude Agent SDK is **not** imported or installed; a deployment must inject a loop
  (M4). Without one the executor role fails closed.
* The git handoff pushes with `--receive-pack="git -c core.hooksPath=/dev/null receive-pack"`; a test found that `-c`/`GIT_CONFIG_*` alone did **not**
  stop the exchange repository's own hooks from running on push. This option does (tested).
* `needs_input` responses park the task in `blocked(needs_input)`; only a human resumes it (never auto-retried), per PROPOSAL §8.5.
* Availability (credentials, price, configuration) is checked **before** budget; a real role then has a $0 role budget until a human grants one.
* `logical_name` patterns now require an alphanumeric first character (rejects `..`, `.hidden`).
* New audit events: `adapter.response_received`, `adapter.response_rejected`, `agent.route_failed` (existing), `pricing.flag`, `usage.divergence` (M2 fix 6).
* Default price table gained a `handoff` entry (0/0) and was re-pinned; no real model prices are shipped (they are business inputs).

## Verified by tests vs. not verified

| Verified by tests (offline, in CI) | Not verified / needs containers or live credentials |
|---|---|
| Request shape our adapters send (URL, credential header present, body keys) **against synthetic fixtures** | That OpenAI/Anthropic accept exactly these parameters; live response fields; rate limits; real error bodies |
| Fail-closed parsing: malformed JSON, missing/wrong-typed fields, truncated, refused, oversized body, 4xx/5xx retry policy | Behaviour of `UrllibTransport` on a real network (TLS, redirects, timeouts); it is never called successfully |
| Host allowlist per adapter; https only; credentials read from env at call time, never stored/printed | Provider-side key scoping and spend caps |
| Executor tool set derives only from the contract's `allowed_actions`; loop tool calls outside it fail closed; outputs safe/declared/bounded | The Claude Agent SDK, its real tool behaviour, and any containment of what tools can do (needs M4 sandbox + Docker) |
| Prompt-injection cases (artifact text, model output, forged closing tag) do not change tools, approvals, budgets or contract hash | Resistance of a *real* model to injection (we only prove the control plane does not grant authority from text) |
| Git handoff: deterministic idempotent dispatch, allowlisted author, bad/absent signature handling, replay protection, schema, size, hash, missing/undeclared outputs, inert non-executable storage, repo hooks do not run | Remote GitHub (tokens, branch protection, web-flow/SSH signatures, rate limits, webhooks/schedules of the real ChatGPT Work product) |
| Signature check with a throwaway local gpg key (good, unknown-key, unsigned, `require_signature`) | Trust policy for real signers, key rotation/revocation |
| Planted secrets redacted before hashing/storage for all four adapters and the generic ingest path; absent from DB/WAL bytes, CAS, audit events, exchange repo requests | Secrets in places we do not control (provider logs, GitHub history of the external side) |
| Malformed / oversized / missing-artifact / hash-mismatch / policy outcomes are rejected and AUDITED for mock and non-mock adapters | Hostile adapters that stall in C code or exhaust memory inside the process (needs process/container isolation) |
| Routing, budget ($0 default, grant flow, token-priced settlement) and review/permission rules are the same matrices for mock and real roles | Real cost accuracy (prices are placeholders in tests; the shipped table has none) |
| AI Scientist v3 cannot be enabled (stub fails closed, audited) | Any isolation boundary for it (M5) |
| Zero network calls in the entire suite (socket guard) | Anything that requires reaching a service |
| Timeouts/leases under a fake clock | The real-time `Worker._join` heartbeat loop with the system clock (only its seam is exercised) |
