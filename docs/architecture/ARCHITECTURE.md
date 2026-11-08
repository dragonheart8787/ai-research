# Architecture (current state)

This describes the repository **as it is now**. [`PROPOSAL.md`](PROPOSAL.md) is the original design (with amendments); where it differs, this file and the ADRs
win. "Tested" below means a test in `tests/` exercises it offline; "not verified" means no test in this repository does.

## Shape
A single Python package, `airos`, is the **control plane**: it owns all state in SQLite, enforces who may do what, and runs agents only through adapters.
Humans are the only approvers. Agents never get API access to the control plane and never see the operator token. There is no Rust, no network call and
no container in the test suite.

```
Human ──(operator token, loopback API)──► api/app.py ──► SqliteRepository ◄── Worker ──► AgentRegistry ──► adapter (mock | real)
                                              │            │  ▲                              │
                                              │            │  └── hash-chained audit log      └─ real adapters: CliRunner / AgentLoop seam
                                              │            └── budgets/ledger, approvals, leases, artifacts registry     (fixture | container; host refused)
                                              └── read-only status + audit verify
```

## Repository directory structure
```
README.md                      overview + how to run in mock mode
pyproject.toml                 build, pytest (warnings are errors), ruff, mypy --strict
requirements/dev.lock          hash-locked environment (uv); scripts/check_lock.py verifies it
config/
  agents.toml                  per-role settings, NO secrets; every real role disabled; attestation false
  egress.toml                  egress allow-list as data, default deny (host names are assumptions)
  pricing.example.json         price-table example (schema airos-prices/2)
docs/
  adr/                         ADR-0001..0014 (see its README)
  architecture/                PROPOSAL, ARCHITECTURE (this), INTERFACES, ROADMAP, M1..M4A notes, M4B-HANDOFF, M4B-DESIGN-RECORD
docker/test-image/Dockerfile   throwaway busybox image for the (skipped) real-container tests
scripts/                       mock_demo.py, sync_env.sh, check_lock.py
src/airos/
  domain/                      pure logic: schemas (contracts), state machine, guards, limits
  persistence/                 SqliteRepository, budget SQL, migrations 0001..0009, interfaces
  agents/                      adapters (mock), registry (gates), worker (leases, heartbeats, timeouts), runner
  agents/real/                 cli (CliRunner seam), cli_adapters (Planner, Director), claude_executor, handoff (git), factory, common
  sandbox/                     spec (container run specs), egress, runner (container CLI runner), checks, live_check (fail-closed stub)
  security/permissions.json    the action x principal matrix
  api/app.py                   FastAPI: operator-token mutations, read-only status
  auth.py config.py pricing.py redaction.py artifacts.py audit.py verifier.py quota_probe.py clock.py canonical.py errors.py
tests/                         unit, integration, e2e-mock, security; fixtures/ (synthetic only), golden/sandbox/ (spec-only goldens), vectors/
```
Planned items in PROPOSAL §12 that do **not** exist: `containers/`, `migrations/` at top level (migrations live in `src/airos/persistence/migrations`),
`schemas/` (JSON Schemas are not generated), `cli/` (there is no `airos` command; use `scripts/mock_demo.py` or the Python API), `.github/workflows/`.

## Components
| Component | Where | Status |
|---|---|---|
| Task state machine, guards, limits | `domain/` | tested |
| SQLite persistence, CAS-style versioning, migrations | `persistence/` | tested |
| Hash-chained audit log with anchors | `audit.py`, repo | tested |
| Authorization matrix; denial audit | `security/permissions.json`, repo | tested |
| Approvals bound to contract version + hash; humans only | repo | tested |
| Budgets: task/root/global + `agent_sdk_credit` pool and role allocations; reconciliation; credit exhaustion | repo, `persistence/budget_sql.py` | tested |
| Worker: leases, heartbeats, wall-clock timeouts, recovery | `agents/worker.py` | tested (fake clock; one real-clock heartbeat test) |
| Artifacts: content-addressed store, lineage, orphaning | `artifacts.py`, repo | tested |
| Mock adapters for all five roles | `agents/adapters.py` | tested; used by the demo |
| Real adapters (Planner `claude -p`, Director `codex exec`, Executor SDK loop, ChatGPT Work git handoff) | `agents/real/` | tested **against synthetic fixtures only**; the real CLIs' behaviour is not verified; all disabled by default |
| AI Scientist | `agents/real/factory.py` | fail-closed stub; cannot be enabled |
| Container run specs, egress policy, container runner, check runner | `sandbox/` | spec/policy logic tested; **isolation and enforcement not verified** (no daemon) |
| Live contract check | `sandbox/live_check.py` | stub that cannot succeed |

## Authentication model (ADR-0011)
`AuthMode` is `subscription_oauth` or `manual_handoff`; there is no API-key mode and no code path that reads a provider API key. Adapter environments are built
from an allowlist (`auth.build_adapter_env`), never inherited; forbidden variable names are refused and audited. Claude-driven roles refuse to start until the
human sets `usage_credits_attested_off = true` (with a date) in `config/agents.toml`; the system cannot verify that setting. After every real run the reported
auth path is compared with the expected one; a mismatch holds the task for a human.

## What no test here exercises
Real provider CLIs; any container, rootless Docker, egress blocking, cgroup limits; token handling by a real CLI; the clock behaviour reported in
`M4B-DESIGN-RECORD.md`. See ROADMAP and ADR-0013/0014.
