# ADR-0011: Subscription-only authentication (no provider API keys)

- Status: Proposed (human directive, M3 review, 2026-11-07)
- Date: 2026-11-07

## Context
The operator uses a Claude Pro plan and a ChatGPT login. Billing risk is the dominant operational risk: an API key silently present in an environment
turns a capped subscription into uncapped pay-as-you-go spend. Earlier milestones allowed per-role API keys (HTTP adapters); that is withdrawn.

## Decision
1. **Global invariant:** the system never uses provider API keys. `auth_mode` is only `subscription_oauth` or `manual_handoff`; `api_key` does not exist.
   Raw HTTP transports for OpenAI/Anthropic were deleted.
2. **Individual use only.** One operator's own subscription. No sharing of accounts or tokens, **no proxying**, **no bridging** the subscription into
   third-party tools, services or other people.
3. **Provider terms are re-checked by the human before enabling any adapter**, and again when a provider changes its terms or plans. The system cannot check this.
4. **Only the Agent SDK monthly credit is used.** "Usage credits" (overflow billing at API rates) must stay **OFF**, so the plan credit is the
   provider-side hard cap. The system cannot read that setting; it requires a human attestation (`usage_credits_attested_off`, with a date) and
   every Claude-driven adapter refuses to start without it. The attestation is a control, not a verification.
5. **Explicit minimal subprocess/container environments** built from an allowlist. Forbidden variables are never passed (ANTHROPIC_API_KEY,
   ANTHROPIC_AUTH_TOKEN, OPENAI_API_KEY, CODEX_API_KEY, GEMINI_API_KEY, Bedrock/AWS credentials, base-URL overrides). A config asking to pass one is
   refused and audited. If such variables exist on the host they are reported (names only) and ignored.
6. **Credentials:** `CLAUDE_CODE_OAUTH_TOKEN` exists only inside a Claude-driven adapter's own container environment (one token per adapter).
   Codex login state lives in a dedicated volume mounted only into the Director container.
7. **Auth-path check after every run:** the reported auth path must be `subscription_oauth`; a mismatch or an unconfirmed path raises
   `billing.integrity_mismatch` and holds the run for human review.
8. **Codex risk:** an older Codex login flow could auto-create a **billable API key** when signing in with an OpenAI account. The Director therefore
   stays disabled unless (a) `codex login status` reports a ChatGPT login and (b) the human attests no API key was created in the account.
9. **Recommendation:** keep usage credits OFF; set a provider-side spending limit of 0 where the provider offers one; review the account page weekly
   and record a reconciliation (see ADR-0013 notes in M4A-NOTES).

## Token revocation runbook (summary; full steps in ADR-0005)
Disable the role in `config/agents.toml` -> revoke the token / sign the session out at the provider -> delete the host variable or the role's login
volume -> search audit rows, artifacts and logs for the old token prefix -> issue a NEW credential for that role only and redo the attestation.

## Consequences
+ The worst realistic cost is bounded by the plan credit, not by a card.
+ No credential shaped like a billing key can reach an agent by accident.
- Throughput is limited by plan limits; credit exhaustion blocks tasks (`blocked(credit_exhausted)`, never auto-retried) until a human acts.
- Terms of service compliance and the "usage credits OFF" setting are human responsibilities that tests cannot verify.

## Alternatives
- **API keys with provider spend caps:** rejected by the operator (billing risk).
- **A local proxy that re-exports the subscription:** prohibited (sharing/proxying).
