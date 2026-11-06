# ai-research

Autonomous AI Research Operating System: a multi-agent control plane whose first goal is a reliable, auditable orchestration system (it produces no research results).

- Design: [`docs/architecture/PROPOSAL.md`](docs/architecture/PROPOSAL.md), decisions in [`docs/adr/`](docs/adr/README.md).
- Status: **M1 (domain + persistence)** implemented; see [`docs/architecture/M1-NOTES.md`](docs/architecture/M1-NOTES.md).

## Develop (Python 3.11+)

```
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # no network, no containers
.venv/bin/ruff check src tests && .venv/bin/mypy src
```
