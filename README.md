# ai-research

Autonomous AI Research Operating System: a multi-agent control plane whose first goal is a reliable, auditable orchestration system. It produces no research
results. Everything runs in **mock mode** by default; real adapters are disabled and are not enabled by anything in this repository.

## Documentation
| Document | What it is |
|---|---|
| [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) | current architecture, directory structure, what is and is not tested |
| [`docs/architecture/INTERFACES.md`](docs/architecture/INTERFACES.md) | task contract, agent request/response schemas, auth modes, `manual_handoff` |
| [`docs/architecture/ROADMAP.md`](docs/architecture/ROADMAP.md) | integration roadmap and what is blocked |
| [`docs/architecture/PROPOSAL.md`](docs/architecture/PROPOSAL.md) | original design (amended; ARCHITECTURE wins where they differ) |
| [`docs/adr/`](docs/adr/README.md) | decision records |
| `docs/architecture/M1..M4A-NOTES.md`, `M4B-HANDOFF.md`, `M4B-DESIGN-RECORD.md` | per-milestone notes; the last two are historical/unverified (see ADR-0014) |

## Status
M1 (domain + persistence), M2 (governance, mock agents, minimal API), M3 (real adapters against synthetic fixtures, git handoff), the subscription-only
authentication change, and M4a (container specs and policy, **isolation not verified**) are implemented and tested offline. M4b and M5 are **blocked on a
revocable credential path**; see the roadmap. No provider CLI, container or network is used by the test suite.

## Run locally in mock mode (no credentials, no network, no containers)
Needs Python 3.11 and [`uv`](https://docs.astral.sh/uv/) (the hash-locked environment is compiled for 3.11).
```
bash scripts/sync_env.sh                     # creates .venv exactly from requirements/dev.lock and verifies it
.venv/bin/python -m pytest -q                # expected: 708 passed, 6 skipped (the 6 are real-container tests, skipped without a Docker daemon)
.venv/bin/ruff check src tests && .venv/bin/mypy --strict src
.venv/bin/python scripts/mock_demo.py        # one task: create -> approve -> run (mock) -> human accept; prints the audit-chain verdict
```
Expected demo output ends with `after human accept: state=completed` and `audit chain ok=True`. Mock output is placeholder data.

Alternative without `uv`: `python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev,api]'`. This does **not** use the lockfile, so
`tests/test_lock_check.py` fails by design (unlocked package/version drift); everything else passes. It also leaves `src/airos.egg-info`, which makes that test
fail even after a locked sync until you delete it.

The HTTP API (`airos.api.app.create_app`, loopback only, operator token from `AIROS_OPERATOR_TOKEN`) is a library function, not a command; there is no `airos` CLI.
Do not set any provider credential for mock mode; none is read.
