# Architecture Decision Records

Format: Status / Context / Decision / Consequences / Alternatives. Date of all initial ADRs: 2026-11-06. Status of all: **Proposed** (becomes *Accepted* when the human replies "approved"). Parent document: [`../architecture/PROPOSAL.md`](../architecture/PROPOSAL.md).

| ADR | Decision |
|---|---|
| [0001](0001-sqlite-with-postgresql-path.md) | SQLite now, repository layer, PostgreSQL migration path |
| [0002](0002-in-process-worker-with-queue-interface.md) | In-process worker now, `WorkQueue` interface for a dedicated queue later |
| [0003](0003-append-only-hash-chained-audit-log.md) | Append-only, hash-chained audit log |
| [0004](0004-github-handoff-for-chatgpt-work.md) | GitHub as the artifact handoff for ChatGPT Work |
| [0005](0005-ai-scientist-isolation-boundary.md) | AI Scientist v3 isolation boundary |
| [0006](0006-docker-as-isolation-layer.md) | Docker as the isolation layer |
| [0007](0007-rust-vs-python-components.md) | Rust vs Python for sandbox runner and audit verifier |
| [0008](0008-humans-only-approvals-and-no-agent-api-access.md) | Humans are the only approvers; agents have no API access |
