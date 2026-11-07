# ai-research

Autonomous AI Research Operating System: a multi-agent control plane whose first goal is a reliable, auditable orchestration system (it produces no research results).

- Design: [`docs/architecture/PROPOSAL.md`](docs/architecture/PROPOSAL.md), decisions in [`docs/adr/`](docs/adr/README.md).
- Status: **M1** (domain + persistence) and **M2** (governance: budgets, authorization, approvals, artifacts, mock agents, minimal API) implemented;
  see [`M1-NOTES.md`](docs/architecture/M1-NOTES.md), [`M2-NOTES.md`](docs/architecture/M2-NOTES.md); **M3** (real adapters behind fixtures, git handoff) in [`M3-NOTES.md`](docs/architecture/M3-NOTES.md).

## Develop (Python 3.11+)

```
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev,api]'      # or: bash scripts/sync_env.sh (hash-verified, exact lock)
.venv/bin/pytest          # no network, no containers
.venv/bin/ruff check src tests && .venv/bin/mypy src
```
