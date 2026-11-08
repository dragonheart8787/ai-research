# ADR-0010: Real adapters ship fixture-tested, network-seamed and fail-closed

- Status: Proposed
- Date: 2026-11-07

## Context
M3 (human-directed scope) adds real adapters for the Director (OpenAI), Planner (Anthropic), Executor (Claude Agent SDK) and the ChatGPT Work
git handoff. There are no credentials, no network in CI, and no container host. Code that talks to paid services must not be able to run by
accident, must not be trusted because it "looks right", and must not leak credentials or take authority from model/artifact text.

## Decision
1. **Transport seam.** Adapters never open sockets. They call a `Transport` (`airos/agents/real/transport.py`). The only network-capable code is
   `UrllibTransport` (https only, no redirects, size-capped); no test calls it successfully, and a suite-wide guard turns any connect/resolve into a failure.
2. **Fixtures, honestly labelled.** Adapters are developed against hand-written fixtures that mirror documented shapes (`tests/fixtures/README.md`).
   They are contract tests of *our* request shape and *our* fail-closed parsing, not proof that a provider accepts them. `RecordingTransport` lets a human
   capture real exchanges later; recorded files are marked as such and never contain credential headers.
3. **Fail closed at the registry.** A real adapter is built only if: enabled, credentials present in the environment (for roles that need one),
   its model has an entry in the pinned price table, and the deployment passed in the transport / loop / handoff configuration. Otherwise routing raises
   `AdapterUnavailable` and writes `agent.route_failed`. Availability is checked before any budget is touched; real roles then have a $0 role budget until
   a human grants one.
4. **Responses are untrusted.** Every response is validated (`AgentResponse`, size caps, hash recomputation, declared outputs only, safe paths).
   Malformed / oversized / missing-artifact / hash-mismatch / policy outcomes fail closed and write `adapter.response_rejected`.
5. **Authority comes only from the contract.** The executor tool set is a static function of `allowed_actions`; prompts carry artifact text only inside
   marked, neutralised data blocks; the loop's reported tool calls are re-checked.
6. **Secrets.** Credentials are read from environment variables at call time, registered with the redactor, never stored on adapters, and redacted from
   outputs before hashing/storage.
7. **AI Scientist v3 stays a stub** that always fails closed (ADR-0005; M5).
8. **Handoff is local-git-only in M3.** Remote URLs are refused. Results are read from git objects, never executed, stored `untrusted`, and always
   require a human reviewer.

## Consequences
+ The whole integration surface is testable offline and cannot spend money or leak keys by accident.
+ Adding a real provider is a factory + a fixture set + a price entry + a human-granted budget.
- Fixtures can drift from reality; a human-run live contract check is a prerequisite for enabling any real adapter.
- The Claude Agent SDK itself, its container and its egress policy are not exercised at all (M4).

## Alternatives
- **Call live APIs in CI with a test key:** rejected (rule: zero network, no secrets in CI).
- **Mock only until M4/M5:** rejected by the human's M3 scope; the seam keeps the risk small.
- **VCR-style auto-recording in tests:** rejected; it normalises hitting the network.
