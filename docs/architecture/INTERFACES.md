# Agent interface specifications (current schemas)

Source of truth: `src/airos/domain/schemas.py` (Pydantic v2, `extra="forbid"`). The tables list fields as of this commit; `tests/test_schemas.py` exercises
validation. Nothing here is a wire protocol to a provider: adapters talk to CLIs or git, not to these models.

## TaskContract (`schema_version = "task-contract/1"`) - what a human or planner creates
| Field | Type / default | Notes |
|---|---|---|
| idempotency_key | str | creating the same key twice returns the same task; a different body with the same key is a conflict |
| kind | `work` \| `objective` (default `work`) | |
| research_objective | str | the only instruction source for an agent |
| assigned_role | `director` \| `planner` \| `executor` \| `chatgpt_work` \| `ai_scientist` | |
| input_artifact_refs | list of `{artifact_id, sha256}` | pinned; orphaned/removed artifacts are refused |
| expected_output | list of `{logical_name, media_type, artifact_class, max_bytes=26214400}` | |
| acceptance_criteria | list of `{id, description, mode: machine\|reviewer\|human, check?}` | `check` types the verifier can run: `file_exists`, `sha256_equals`; others fail closed |
| resource_limits | `max_tokens=200000, max_usd_micro=2000000, wall_clock_s=900, container_cpu_millis=2000, container_mem_mib=4096, max_retries=2, external_deadline_s?` | |
| approval_requirements | list of `{type, reason}`; types: `execute, cost, destructive, external_handoff, budget_increase, limit_increase, artifact_release` | each needs a human grant bound to task version + contract SHA-256 |
| side_effect_class | `none` (default) \| `workspace_write` \| `external_write` \| `destructive` | side-effectful tasks are never re-run automatically after a crash |
| human_review_required | bool = false | |
| allowed_actions | list[str] | the ONLY source of an Executor's tool set (static table; unknown actions grant nothing) |

## AgentRequest (`agent-request/1`) - what an adapter receives
`task_id, run_id, attempt, role, research_objective, input_artifacts (refs), expected_output, acceptance_criteria, resource_limits, allowed_actions,
lease_expires_at_ms`. Built by `build_agent_request` from the approved contract; input artifact bytes are read through a reader bound to the agent's read
permissions and are placed in prompts only inside marked, untrusted-data blocks.

## AgentResponse (`agent-response/1`) - what an adapter returns
| Field | Notes |
|---|---|
| task_id, run_id, attempt | must match the request |
| status | `succeeded` \| `failed` \| `needs_input` \| `refused` |
| retryable | bool; `credit_exhausted` is never retryable |
| produced_artifacts | list of `{logical_name, path, sha256, size_bytes, media_type, artifact_class, parent_artifact_ids}`; the worker recomputes hashes from bytes |
| resource_usage | `{tokens_in, tokens_out, usd_micro, wall_ms, cpu_ms?, peak_mem_bytes?, measured_by, model?}`; the control plane **ignores** adapter USD and estimates from tokens x the pinned price table |
| acceptance_results | `{criterion_id, self_reported: pass\|fail\|not_applicable, evidence_ref?}`; self-reports are never trusted |
| evidence | list of check items; verified through the check runner; un-verifiable evidence fails the response |
| notes, error | `error = {ts_ms, attempt, error_class, retryable, detail}`; classes include `transient, schema_violation, timeout, policy, handoff_invalid, hash_mismatch, credit_exhausted` |
| **auth_path** | how the provider says it authenticated (`subscription_oauth`, `manual_handoff`, `unconfirmed:<source>`, or `None` = unconfirmed). Compared with the adapter's expected `auth_mode` after every real run; a mismatch or `None` holds the task for a human |

## Authentication modes (per role, `config/agents.toml` `auth_mode`)
| Role | Allowed `auth_mode` | Credential |
|---|---|---|
| planner, executor | `subscription_oauth` | a per-role token injected as `CLAUDE_CODE_OAUTH_TOKEN` into that role's sandbox only; host variable name from `credential_env` |
| director | `subscription_oauth` | Codex ChatGPT login state in the Director's own volume; needs a human attestation that no API key was auto-created; login mode re-checked every run |
| chatgpt_work | `manual_handoff` | none held by us |
| ai_scientist | `subscription_oauth` (nominal) | stub; cannot be enabled |
`api_key` does not exist: configuration naming it is refused. All real roles default to `enabled = false`. Everything about the CLIs' flags, output fields and
credential-source reporting is an **assumption** exercised only against synthetic fixtures.

## manual_handoff (ChatGPT Work), as implemented
Local git repository as the exchange medium (remote URLs are refused). Outbound: the adapter commits `requests/<task_id>/request.json` (schema
`handoff-request/1`, redacted, deterministic so a re-dispatch is a no-op), `request.md` and `inputs/*`, then parks the task in `blocked(needs_input)`. A human or an
external workflow replies by committing `results/<task_id>/response.json` (schema `handoff-result/1`: `task_id, request_sha256, status, outputs[], notes`) plus
`results/<task_id>/outputs/<name>`. Ingest reads git objects only (no checkout, no execution) and checks: author on the allowlist (author metadata is
forgeable; repository access control is the real defense), signature status (`require_signature`, default false: unsigned results need an allowlisted author **and**
mandatory human review), replay protection via `request_sha256`, per-file hashes and sizes, secret redaction. Results are stored `untrusted`. The system cannot
resume a parked handoff; only a human can.

## Adapter execution seam (real adapters)
`CliRunner.run(CliInvocation) -> CliResult` with kinds `fixture | container | host`. Real adapters accept only `fixture` and `container` (and
`control_plane` for git); `host` and in-process execution are refused, and the worker re-checks the kind. The Executor uses an `AgentLoop` with the same rule.
Prompts travel over stdin, never argv; the adapter environment is exactly `auth.build_adapter_env(role, credential)`.
