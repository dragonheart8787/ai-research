# Autonomous AI Research Operating System — Architecture Proposal

- Status: **PROPOSED — awaiting human reply "approved"**
- Date: 2026-11-06
- Scope of this document: environment inspection and design only. No implementation code, no installs, no external API calls, no containers, no GPU/training work were performed or are authorised by this document.
- Codename used below: `airos` (Python package), "control plane" = the single FastAPI process that owns state.

**Amendments (2026-11-06, human review "approved with changes"):** see ADR-0009 (approval binding to task version + contract hash), the revised §5.3 anchoring, §7 operator-token authentication, §11.2 Q6 decision, §4.3 concurrency/Clock notes and §11.4 untrusted-input rule. Where this text and an ADR disagree, the newer ADR wins.

Conventions: **MUST / MUST NOT** are normative and each is meant to be testable. Every numeric default is a *proposed default*, configurable in `config/limits.toml` unless marked **HARD** (not configurable without a code change and a new ADR). Open questions are collected in §15.2; nothing ambiguous is silently decided.

---

## 0. Environment inspection (read-only, performed 2026-11-06)

| Item | Finding | Verdict |
|---|---|---|
| OS | Ubuntu 24.04.5 LTS, Linux 6.18.44, x86_64 | OK |
| Python | `python3` = 3.13.18 (`/usr/local/bin`); also `/usr/bin/python3.11`, `/usr/bin/python3.12` | OK (≥3.11). Target support: 3.11+; CI matrix 3.11 and 3.12; no 3.13-only syntax |
| Docker client | `/usr/bin/docker`, Docker Engine - Community 29.9.2 | Client present |
| Docker daemon | **Not running / not reachable**: `/var/run/docker.sock` does not exist | **Missing.** Containers cannot be started in this environment |
| Docker rootless | **Cannot be determined** (no daemon to query). Session user is `uid=0 (root)`; rootless mode is therefore not in use here | **Unknown**; treated as *not rootless* for planning |
| GPU | `nvidia-smi` absent | None. All GPU work is a non-goal |
| Git | Branch `main`, tracking `origin/main`, one commit (`9f7dbb2 Initial commit`), working tree clean before this task | OK |
| Git remote | `origin` = `https://github.com/dragonheart8787/ai-research` (fetch+push) | OK |
| Repo contents | Only `README.md` (13 bytes: `# ai-research`) and `.git`. No `docs/`, no `.github/`, no code, no CI | Greenfield |
| Disk | 31 GiB available on `/` (252 GiB nominal, 8.5 GiB used; the writable allowance is what matters) | Sufficient for design + M1–M3 |
| CPU / RAM | 4 vCPU, 15 GiB | Sufficient for M1–M3 |
| SQLite | 3.53.1 via Python `sqlite3` (WAL, JSON1, partial indexes, `RETURNING` available) | OK |
| Other toolchains | Node 22 and Rust (`rustc`/`cargo`) present; `fastapi`/`pydantic` **not installed** | Python deps missing — expected; install deferred to M1 after approval |
| Network | Outbound HTTPS only via a pre-configured agent proxy | No direct network assumptions |
| Secrets | None configured for OpenAI/Anthropic/GitHub-handoff (not inspected beyond absence of requirement; none will be read) | All adapters start as mocks |

**What is missing:** (1) a running Docker daemon (so the sandbox runner, Executor container and AI Scientist container cannot be exercised here); (2) Python dependencies; (3) any credentials; (4) a CI configuration; (5) a decision on where final-evaluation data lives on a real host.

**What I will assume:** (a) M1–M3 are developed and fully tested here with mocks and no Docker; (b) M4–M5 (real containers) require a host with a Docker daemon, preferably rootless or `userns-remap`, supplied by the human; (c) the single control-plane process runs on one host; (d) one human operator initially (multi-human supported by schema); (e) no model names are hard-coded — models are chosen in config.

---

## 1. Goals and non-goals

### Goals
- G1. A **reliable, auditable orchestration system**: every task transition is persisted, authorised, and recorded in a tamper-evident log.
- G2. **Deterministic control**: a task state machine with explicit guards; illegal transitions are impossible at both the service layer and the database layer.
- G3. **Human-gated cost and destruction**: only humans grant approvals and budget increases.
- G4. **Crash-safe**: kill -9 at any point leaves the system recoverable with no duplicate execution and no double-charge.
- G5. **Pluggable agents**: five roles behind one adapter interface; everything ships as mocks first.
- G6. **Provenance**: every artifact carries sha256, producer run, parents, creation time.
- G7. **Containment**: agent-generated code runs only in restricted containers; AI Scientist v3 is isolated from the Executor pipeline.

### Non-goals
- NG1. **Producing any research result.** The first milestone delivers orchestration, not science. Mock agents emit synthetic, flagged (`is_mock=true`) artifacts.
- NG2. No GPU scheduling, training, or hosting of self-hosted GPU runners.
- NG3. No multi-host/distributed deployment, no Kubernetes, no PostgreSQL in the first release (only the migration path).
- NG4. No autonomous spending: no agent may raise a budget, approve work, or self-approve its output.
- NG5. No assumption that ChatGPT Work exposes a callable API.
- NG6. No polished UI: a minimal read-only status page only.
- NG7. No public exposure of the API (binds to `127.0.0.1` by default).

---

## 2. Components

```
                         ┌──────────────────────────── control plane (one process) ───────────────────────────┐
 Human (CLI / API) ─────►│ FastAPI  ──► Services ─────────────► Repository layer ─────► SQLite (WAL)            │
                         │  (authn,     ├ TaskService (state machine)   (interfaces,        ├ tasks/runs/...    │
                         │   authz)     ├ ApprovalService                 SQL only)         ├ task_events (chain)│
                         │              ├ BudgetService (ledger)                            └ budget_ledger     │
                         │              ├ ArtifactRegistry ───────────► CAS store (files, sha256-addressed)     │
                         │              ├ AuditLog (hash chain)                                               │
                         │              └ Verifier (deterministic checks)                                     │
                         │ Scheduler/Worker loop (in-process) ── QueueInterface ──► Adapter registry          │
                         └───────────────────────────────────────────────────┬────────────────────────────────┘
                                          mock adapters │ real adapters (only if explicitly configured)
        Director(OpenAI) · Planner(Anthropic) · Executor(Claude Agent SDK, container) · ChatGPT-Work(GitHub handoff) · AI-Sci-v3 (isolated container)
```

| # | Component | Responsibility | Key constraints |
|---|---|---|---|
| C1 | **FastAPI control plane** | Only writer of state. Exposes `/v1/*`. Binds `127.0.0.1`. Mutating human endpoints (approve, deny, cancel, budget grant, release) require the **operator token**, read from env var `AIROS_OPERATOR_TOKEN` at startup (compared in constant time; only its sha256 is ever kept in memory/logs; never stored in the DB or repo). Status/dashboard endpoints are **read-only** (GET only). Agents never receive this token (it is not passed to adapters or containers). | Agents never call it (see ADR-0008). Mutating endpoints require `principal.type=human` unless listed otherwise in §8. |
| C2 | **SQLite persistence** | WAL, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`, single writer (control plane). | Plain SQL through a **repository layer**; one `Repository` Protocol per aggregate; no ORM. Portable subset only (§5.0). See ADR-0001. |
| C3 | **Agent adapters** | Uniform interface (§4.1). Mock and real implementations. | Real adapter activates only if explicitly enabled **and** credentials present; otherwise mock. |
| C4 | **Scheduler / worker loop** | Moves `approved→queued→running`, leases, heartbeats, retries, timeouts, recovery. | In-process now (asyncio task + a bounded thread pool for blocking adapters). Talks to a `WorkQueue` Protocol so a dedicated queue can replace it. See ADR-0002. |
| C5 | **Approval service** | Creates approval requests, records human decisions, binds each to `(task_id, task_version, contract_sha256)` (ADR-0009), expires them. | Decider must be `human`; enforced by service **and** DB trigger. |
| C6 | **Budget ledger** | Append-only ledger; reserve → settle; hard stop; human-only grants. | Integer micro-USD; invariant checked in-transaction. |
| C7 | **Artifact registry** | Content-addressed store + provenance + lineage; trust level per artifact. | Files immutable; DB rows immutable (no UPDATE except `trust_level` via release action, which is itself audited). |
| C8 | **Audit log** | `task_events`, append-only, hash-chained. | Triggers deny UPDATE/DELETE. See ADR-0003. |
| C9 | **Verifier** | Re-computes sha256, validates schemas, re-runs deterministic acceptance checks in a separate no-network sandbox/process. | Never trusts self-reported results or usage. |
| C10 | **Status API / dashboard** | Read-only JSON + one server-rendered HTML page (tasks, runs, budgets, audit-chain status). | No mutation from the HTML page in M1–M4. |
| C11 | **Sandbox runner** | Builds the Docker run spec from a validated policy object (§11) and starts/stops containers. | Only module permitted to talk to Docker. No `sh -c` from agent input. |
| C12 | **Secret & redaction layer** | Loads secrets from env/secret store; registers values with `Redactor`; all logs/events/artifacts-text pass through it. | Secrets never written to DB or git. |

---

## 3. Five roles and adapters

All five implement `AgentAdapter` (§4.1). Each role has exactly one `agents` row; `adapter_kind ∈ {mock, real}`.

**Activation rule (applies to all):** default `adapter_kind=mock`. A real adapter is constructed only if **all** hold: (1) `config/agents.toml` has `[role].mode = "real"`; (2) the role's credential env var/secret is present and non-empty; (3) the role has a budget row with `limit_usd_micro > 0` (not required for ChatGPT Work, which has no metered cost to us — it uses limits on count instead); (4) a human has recorded an `adapter.enable` audit event (`airos agents enable <role>`). If mode is `real` but (2)–(4) fail, startup **fails closed** with an error — it MUST NOT silently fall back to mock, because that would make output look real-validated. Mock artifacts carry `is_mock=true`; a task whose adapter is real MUST NOT consume mock artifacts unless `allow_mock_inputs=true` (human-set).

| Role | Backend | Does | Must not | Credentials / network |
|---|---|---|---|---|
| **Research Director** | OpenAI API | Turns a request into an objective brief; reviews completed runs (verdict accept/rework/reject); proposes follow-ups as *drafts*. | Execute work, approve anything, raise budgets, review its own outputs. | `OPENAI_API_KEY`; host process egress to the OpenAI API host only. |
| **Planner** | Anthropic API | Decomposes an objective into a plan: child task contracts, dependencies, estimated limits. | Execute, approve, exceed depth/count limits. | `ANTHROPIC_API_KEY` (planner scope). |
| **Executor** | Claude Agent SDK, **inside a restricted container** | Performs workspace-scoped work (code, analysis, file transforms) per contract. | Network beyond allowlist, host mounts beyond the run workspace, reading `eval_holdout`, spawning containers. | Separate Anthropic key (or proxy-injected, §11.2); egress via allowlist proxy to the Anthropic API host only. |
| **ChatGPT Work Researcher** | **GitHub-based handoff only** (see below) | Produces research notes/literature summaries via a ChatGPT Work workflow that we do not call. | Anything requiring a callable API; being assumed synchronous. | GitHub token scoped to the *handoff repo* only (write requests, read results). No OpenAI credential held by us for this role. |
| **AI Scientist v3** | Third-party research agent, **isolated container** | Runs its own end-to-end experiment pipeline on a request, in complete isolation. | Share network/volumes/credentials/images with Executor; read any control-plane or Executor data; receive secrets other than its own. | Own API key(s), own network, own volumes, own budget (see below). |

### 3.1 ChatGPT Work Researcher — GitHub handoff protocol (no callable API assumed)
1. **Dispatch** (control plane → GitHub). Requires an approval of type `external_handoff` (data leaves the host). Control plane commits, to the *handoff repository* (default: separate private repo `ai-research-handoff`; open question Q3), a directory `requests/<task_id>/` containing `request.md` (human-readable), `contract.json` (the task contract, redacted), and `manifest.json` listing every input artifact with sha256 and a copy of the artifact file. The commit path and content are deterministic, so re-dispatch with identical content is a no-op (idempotent).
2. **Trigger.** The ChatGPT Work workflow is started by (a) a schedule, (b) a GitHub event (push to `requests/**`, label, or issue), or (c) a human manually starting it. We do not control or observe which; the adapter only needs the repo state.
3. **Return.** The workflow writes `results/<task_id>/response.json` (conforming to `AgentResponse` v1, §6.2) plus output files, by commit/PR to branch `results/<task_id>`. Alternative manual path: a human runs `airos artifacts import --task <id> <files…>`; the human principal is then recorded as producer-of-record.
4. **Ingest.** The adapter polls (read-only token, default every 5 min, max 12/h) or the human triggers `airos handoff sync`. Ingest verifies: path is exactly `results/<task_id>/`; `response.json` validates; every artifact's sha256 matches bytes; commit author is on `allowed_handoff_authors` and (if configured) signature is verified; no file exceeds `max_artifact_bytes` (default 25 MiB). Failure → run `failed` with `error_class=handoff_invalid` (never auto-retried).
5. **Trust.** All ingested artifacts have `trust_level=untrusted` (treated as data, never as instructions; excluded from prompts of other agents unless a reviewer marks them `reviewed`). Prompt-injection text inside them has no authority.
6. **Run phase.** The run uses `phase=awaiting_external` with `external_deadline` (default 72 h, **not** the 60 s lease). Timeout → `timed_out`. Never auto-re-dispatched (would duplicate external work).
7. **Mock:** a local directory acts as the "repo"; a script drops a canned `response.json`.

### 3.2 AI Scientist v3 — isolation boundary (summary; full in ADR-0005)
- Separate compose project/Docker network (`sci-net`, `internal: true`), separate image digest, separate named volumes `sci-in`, `sci-work`, `sci-out`, separate API credentials and separate budget row `scope=ai_scientist`.
- Control plane writes only `request.json` (+ declared input files) to `sci-in` (mounted **read-only** in the container). The container writes only to `sci-work` (rw, private) and `sci-out` (rw). The control plane mounts `sci-out` **read-only** on the host side and ingests via the same verifier path.
- No shared network with Executor, no shared volumes, no Docker socket, no shared env, no shared image layers requirement. The Executor and Scientist containers never run in the same compose project.
- Outputs enter registry as class `scientist_output`, `trust_level=quarantined` until a **human** runs `airos artifacts release <id>`.
- Assumption flagged (Q7): "AI Scientist v3" is treated as an untrusted third-party codebase that executes LLM-generated code; license, behaviour, and network needs are to be reviewed before M5.

---

## 4. Task state machine

### 4.1 Task kinds
- `work`: executable by an agent (one `run` per attempt).
- `objective`: container task (root or composite). Has children; never has a `run` row or a lease. In state `running` it means "children are in flight".

### 4.2 States

| State | Terminal? | Meaning |
|---|---|---|
| `draft` | no | Contract being formed; mutable. |
| `planned` | no | Contract complete; `contract_sha256` computed. Changes are allowed in `draft`/`planned`/`awaiting_approval`/`approved` but every change bumps `version` and invalidates all existing approvals (ADR-0009). |
| `awaiting_approval` | no | Pending ≥1 human approval. |
| `approved` | no | All required approvals granted for the current `(version, contract_sha256)`. |
| `queued` | no | Eligible for scheduler; may carry `not_before`. |
| `running` | no | Leased by a worker (work) or children in flight (objective). |
| `review` | no | Output exists and passed machine verification; awaiting verdict. |
| `blocked` | no | Cannot proceed; has `blocked_reason` and `blocked_from`. |
| `completed` | **yes** | Accepted. |
| `failed` | **yes** | Not accepted / unrecoverable / retries exhausted. |
| `timed_out` | **yes** | A time limit ended the task (approval TTL, wall-clock, review TTL, blocked TTL). |
| `cancelled` | **yes** | Stopped by a human or the Director (or cascade). |

Terminal states have **no outgoing transitions** (enforced by DB trigger). A human "retry" of a terminal task creates a **new** task with `retry_of_task_id` and a new idempotency key (and goes through approval again).

`blocked_reason ∈ {budget_insufficient, dependency_failed, needs_input, adapter_unavailable, crash_recovery_decision, budget_exhausted}`.

### 4.3 Transition table (complete; any pair not listed is illegal)

Triggers: **H** human, **S** system (control-plane code, deterministic; never an LLM), **D** Research Director, **P** Planner. "Guard" must hold at commit time inside the same DB transaction as the transition and its audit event.

| ID | From → To | Trigger | Guard conditions | Effects |
|---|---|---|---|---|
| T01 | draft → planned | P, H | Plan/contract validates against schema v1; `depth ≤ 3`; `children ≤ 8`; `root_total ≤ 25`; Σ child `max_usd` ≤ parent `max_usd`; `contract_sha256` computed; `side_effect_class` set | freeze spec; create child tasks in `draft`→(they receive plans by P in turn); event `task.planned` |
| T02 | draft → cancelled | H, D | Task non-terminal | cascade to descendants |
| T03 | planned → awaiting_approval | S | Policy yields ≥1 required approval (§9 rule R-APPROVE) | create approvals `pending`, `expires_at = now+72h` |
| T04 | planned → approved | S | Policy yields **zero** required approvals **and** `side_effect_class=none` **and** `max_usd ≤ auto_approve_usd` (default `0`) | event records `approved_by=policy:auto`; no approval row |
| T05 | planned → cancelled | H, D | — | cascade |
| T06 | awaiting_approval → approved | H | Every required approval bound to the current `version` **and** `contract_sha256` is `granted` by a `human` principal; none `denied`/`expired`/`revoked` | event `task.approved` |
| T07 | awaiting_approval → draft | H | reason ≥ 1 char (request changes) | approvals `revoked`; any re-plan amendment bumps `version` |
| T08 | awaiting_approval → cancelled | H | any approval `denied` or explicit cancel | cascade |
| T09 | awaiting_approval → timed_out | S | `now ≥ expires_at` and approvals still `pending` | approvals `expired` |
| T10 | approved → queued | S | all `depends_on` tasks `completed`; `approval (version, contract_sha256) == task (version, contract_sha256)`; grant age ≤ 7 d; adapter for role enabled | event `task.queued` |
| T11 | approved → awaiting_approval | S | grant age > 7 d before queued | new pending approvals |
| T12 | approved → cancelled | H, D | — | cascade |
| T13 | approved → blocked | S | any dependency `failed`/`timed_out`/`cancelled` | `blocked_reason=dependency_failed` |
| T14 | queued → running | S (worker) | CAS on `version`; `not_before ≤ now`; global running < `max_concurrent_runs` (2) and per-role < 1; **budget reservation succeeds at task, root and global levels** (§10); for `work`: new `runs` row (`attempt = attempt_count+1`, lease set) and `attempt_count` incremented; for `objective`: no run row | reserve ledger entry |
| T15 | queued → blocked | S | reservation fails (`budget_insufficient`) or dependency failed or adapter disabled/missing credential (`adapter_unavailable`) | no reservation held |
| T16 | queued → cancelled | H, D | — | cascade |
| T17 | running → review | S | `work`: valid `AgentResponse` with `status=succeeded`; Verifier passed (all hashes recomputed, schema ok, all `machine` acceptance criteria re-run and passed, usage re-measured); settlement ledger entry written. `objective`: all children `completed` | ingest artifacts + lineage; settle |
| T18 | running → queued | S | retryable error class **or** lease expiry/crash with `side_effect_class ∈ {none, workspace_write}`; `attempt_count < 1+max_retries`; (timeout: `timeout_retries < 1`) | settle (§10); `not_before = now + backoff` (30 s·2^(n-1), cap 10 min, ±20 % jitter) |
| T19 | running → failed | S | non-retryable error; retries exhausted; policy violation; response `status=failed` with `retryable=false`; `objective` with a failed child under `fail_fast` (default) | settle; cascade-cancel remaining children |
| T20 | running → timed_out | S | `wall_clock_s` exceeded **and** timeout retries exhausted (or side-effect class not retryable) | kill container; settle |
| T21 | running → blocked | S | response `status=needs_input`; or budget exhausted mid-run; or crash with non-retryable `side_effect_class` | `blocked_reason` accordingly |
| T22 | running → cancelled | H, D (as cancel request, executed by S) | container stop confirmed (or external run abandoned & recorded) | settle/release |
| T23 | review → completed | D, H (recorded by S) | verdict `accept`; reviewer principal ≠ producing run's agent; Verifier pass stored; if `human_review_required` **or** `assigned_role ∈ {director, planner, chatgpt_work, ai_scientist}` **or** artifacts have `trust_level ≠ trusted`/`is_mock` mix rule → reviewer MUST be `human` | event `task.completed` |
| T24 | review → queued | D, H | verdict `rework`; `review_cycles < 2`; `attempt_count < 1+max_retries`; rework instructions artifact attached | `review_cycles++` |
| T25 | review → failed | D, H | verdict `reject`, or rework limits exhausted | — |
| T26 | review → cancelled | H | — | cascade |
| T27 | review → timed_out | S | in `review` > 72 h | — |
| T28 | blocked → queued | S, H | `blocked_from ∈ {approved, queued, running}`; clearing condition met: `dependency_*` — cannot resume (cancel only); `adapter_unavailable` — adapter enabled (S); `budget_insufficient`/`budget_exhausted`/`needs_input`/`crash_recovery_decision` — **H only** (human grant or explicit resume) | event with resolver |
| T29 | blocked → cancelled | H, D | — | cascade |
| T30 | blocked → timed_out | S | blocked > 7 d | — |

Cancellation of a parent cancels all non-terminal descendants in one transaction. `running→cancelled` for `work` first sets `cancel_requested=1`; transition commits only after container stop is confirmed (or, for `awaiting_external`, abandonment is recorded).

**Enforcement (defence in depth):** (1) pure-function `TaskStateMachine.transition(task, event, principal)` in the domain layer; (2) service layer authz check; (3) DB table `allowed_transitions(from_state,to_state)` seeded by migration + `BEFORE UPDATE OF state` trigger rejecting unlisted pairs and any change from a terminal state; (4) every transition runs inside `BEGIN IMMEDIATE` with compare-and-swap on `tasks.version` (`UPDATE … WHERE task_id=? AND version=?`; 0 rows ⇒ `VersionConflict`). All timestamps are UTC epoch milliseconds obtained from an **injectable `Clock`** (`now_ms()`); no code path reads the wall clock directly, so tests use a fake clock and never sleep.

### 4.4 Limits (retry and recursion)

| Limit | Default | HARD cap | Notes |
|---|---|---|---|
| `max_retries` per task | 2 (→ 3 attempts) | 5 | Contract may lower, never raise above cap |
| `max_review_cycles` | 2 | 3 | Rework also consumes an attempt |
| `max_timeout_retries` | 1 | 1 | |
| `max_subtask_depth` (root = 0) | 3 | 5 | Plan rejected (T01 guard) if exceeded |
| `max_children_per_task` | 8 | 16 | |
| `max_total_tasks_per_root` | 25 | 100 | Counted over all non-`draft`-cancelled descendants incl. retries/supersedes; raising above 25 needs a human approval of type `limit_increase` |
| `max_concurrent_runs` global / per role | 2 / 1 | 8 / 2 | |
| Lease TTL / heartbeat interval | 60 s / 15 s | — | Expiry = 4 missed heartbeats |
| Approval request TTL / approval grant validity | 72 h / 7 d | — | |
| `blocked` TTL / `review` TTL | 7 d / 72 h | — | |
| `max_artifact_bytes` | 25 MiB | 256 MiB | |

---

## 5. Data model

### 5.0 Portability rules (SQLite now → PostgreSQL later)
Plain SQL only; IDs are UUIDv7 as `TEXT` (→ `UUID`); timestamps are `INTEGER` Unix epoch **milliseconds** UTC (→ `BIGINT`); money is `INTEGER` micro-USD (1 USD = 1 000 000); JSON stored as `TEXT` with `CHECK (json_valid(col))` (→ `JSONB`); booleans as `INTEGER CHECK IN (0,1)`; no `AUTOINCREMENT` reliance beyond a single `seq` column; no SQLite-only functions in repositories; partial unique indexes (supported by both). Migrations are numbered `.sql` files with optional `pg/` overrides. Locking: SQLite `BEGIN IMMEDIATE` ↔ PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`; lease claim uses compare-and-set `UPDATE … WHERE state='queued' AND state_version=?` which is valid in both.

### 5.1 Tables (key columns; `*` = NOT NULL, `UQ` = unique)

**principals** — `principal_id*` PK, `type*` (`human|agent|system`), `role` (`director|planner|executor|chatgpt_work|ai_scientist|operator|…`), `display_name*`, `token_hash` (sha256 of bearer token; null for agent/system), `enabled*`, `created_at*`.

**agents** — `agent_id*` PK, `role*` UQ, `principal_id*` FK, `adapter_kind*` (`mock|real`), `adapter_version*`, `config_hash*`, `image_digest` (executor/scientist), `enabled*`, `enabled_by` FK principals, `enabled_at`.

**tasks** — `task_id*` PK; `idempotency_key*` **UQ**; `kind*` (`work|objective`); `root_task_id*`; `parent_task_id`; `depth*` CHECK 0..5; `supersedes_task_id`; `retry_of_task_id`; `assigned_role*`; `state*`; `version*` (CAS counter, starts at 1; bumped on every state change and every amendment); `contract_version*` (starts at 1; bumped only by contract/budget amendments; this is the `task_version` approvals bind to, see ADR-0009); `blocked_reason`; `blocked_from`; `contract_json*`; `contract_sha256*` (sha256 of the canonical contract, which excludes `task_id` and all mutable state); `side_effect_class*` (`none|workspace_write|external_write|destructive`); `attempt_count*`; `max_retries*`; `review_cycles*`; `timeout_retries*`; `not_before`; `cancel_requested*`; `is_mock*`; `created_by*` FK principals; `created_at*`; `updated_at*`. Indexes: `(state, not_before)`, `(root_task_id)`, `(parent_task_id)`.

**task_dependencies** — `task_id*`, `depends_on_task_id*`; PK both; CHECK not equal.

**task_inputs** — `task_id*`, `artifact_id*`, `sha256*` (pinned); PK (task_id, artifact_id).

**allowed_transitions** — `from_state*`, `to_state*`; PK both (seeded; read-only).

**runs** — `run_id*` PK; `task_id*` FK; `attempt*`; `agent_id*`; `state*` (`leased|running|awaiting_external|succeeded|failed|timed_out|cancelled|abandoned`); `lease_owner` (worker UUID, new per process start); `lease_expires_at`; `heartbeat_at`; `external_deadline`; `container_id`; `workspace_ref`; `request_hash*`; `response_hash`; `error_class`; `error_detail` (redacted); `usage_json`; `started_at*`; `ended_at`. **UQ(task_id, attempt)**; **partial UQ(task_id) WHERE state IN ('leased','running','awaiting_external')**.

**task_events** (audit, append-only) — `seq*` INTEGER PK (monotonic, gapless, assigned inside the writer transaction); `event_id*` UQ; `ts*`; `task_id`; `run_id`; `event_type*`; `actor_principal_id*`; `actor_type*`; `from_state`; `to_state`; `payload_json*` (redacted, canonical JSON); `prev_hash*`; `entry_hash*` UQ. Triggers: `BEFORE UPDATE` and `BEFORE DELETE` → `RAISE(ABORT)`.

**approvals** — `approval_id*` PK; `task_id*`; `approval_type*` (`execute|cost|destructive|external_handoff|budget_increase|limit_increase|artifact_release`); `task_version*`; `contract_sha256*` (both immutable, trigger-enforced; an approval is valid only while both equal the task's current values); `requested_by*`; `requested_at*`; `expires_at*`; `status*` (`pending|granted|denied|expired|revoked|invalidated`); `decided_by` FK principals; `decided_at`; `reason`; `amount_json` (for budget/limit). Trigger: on `status IN ('granted','denied')` require `decided_by` principal `type='human'` (**no exceptions**). **Partial UQ(task_id, approval_type, task_version) WHERE status IN ('pending','granted')**.

**budgets** — `budget_id*` PK; `scope*` (`global_daily|root|task|ai_scientist|role`); `scope_ref*`; `limit_usd_micro*`; `limit_tokens*`; `limit_wall_s*`; `state*` (`active|exhausted|frozen`); `created_at*`. UQ(scope, scope_ref).

**budget_ledger** (append-only; UPDATE/DELETE trigger-blocked) — `entry_id*` PK; `budget_id*`; `task_id`; `run_id`; `kind*` (`grant|reserve|settle|release|adjust`); `usd_micro*` (signed); `tokens*`; `wall_s*`; `approval_id` (required for `grant` after initial seed and for `adjust`); `idempotency_key*` **UQ** (`"{run_id}:{kind}"` or `"grant:{approval_id}"`); `created_at*`; `event_seq*` FK to task_events.

**artifacts** — `artifact_id*` PK; `sha256*`; `size_bytes*`; `media_type*`; `artifact_class*` (`request|plan|work_product|external_handoff|scientist_output|eval_result`); `storage_uri*` (CAS path); `logical_name*`; `producer_run_id` FK; `producer_principal_id` FK (set for human imports; exactly one of the two producer columns is non-null); `trust_level*` (`trusted|reviewed|untrusted|quarantined`); `is_mock*`; `provenance_hash*`; `created_at*`. **UQ(producer_run_id, logical_name)**. No UPDATE except `trust_level` (guarded, audited, human for `quarantined→untrusted`).

**artifact_lineage** — `child_artifact_id*`, `parent_artifact_id*`, `relation*` (`derived_from|input_to`); PK all three. Acyclic by construction: a parent must exist before a child is inserted and rows are immutable; trigger asserts `parent.created_at ≤ child.created_at`.

(Held-out evaluation data is deliberately **not** in this schema; see §11.)

### 5.2 Provenance
For every artifact: `provenance = {sha256, producer_run_id | producer_principal_id, parent_artifact_ids (sorted), created_at, is_mock}`; `provenance_hash = sha256(canonical_json(provenance))`. A derived artifact's declared parents MUST be a subset of the run's pinned inputs (`task_inputs`) plus artifacts the same run already produced; the Verifier rejects other claims. Verification at any time: recompute sha256 of the file, recompute `provenance_hash`, confirm each parent exists.

### 5.3 Audit hash chain
- **Canonical form:** JSON per RFC 8785 (JCS): UTF-8, keys sorted, no insignificant whitespace, integers only (no floats; money in micro-USD).
- **Entry hash:** `entry_hash = sha256( prev_hash_hex ‖ 0x0A ‖ canonical_json(entry_without_prev_hash_and_entry_hash) )`, where `entry_without_…` includes `seq, event_id, ts, task_id, run_id, event_type, actor_*, from_state, to_state, payload_json`. Genesis `prev_hash = "0"×64`.
- **Single chain per database**; `seq` gapless. Writes serialised by `BEGIN IMMEDIATE`; the event row is written in the **same transaction** as the state change it records (no state change without an event, no event without its state change).
- **Redaction before hashing:** payloads are redacted first; the hash covers the redacted bytes.
- **Verification:** `airos audit verify [--from seq]` recomputes the chain and checks gapless `seq`; exposed at `GET /v1/audit/verify`. Startup verifies the last 1 000 entries (full verify if total ≤ 100 000).
- **Anchoring (limits of tamper evidence):** a chain inside the same DB detects partial edits but not a full rewrite by someone who can rewrite the whole DB. The head `(ts, seq, entry_hash, event_type)` is appended (one line, fsync'd) to `anchors/audit-anchors.log` on a separate volume **on every human approval decision and on every root-objective completion**; a human commits that file to git weekly. There is no automatic push and no time-based anchoring.
- Test vectors for the chain (input events → expected hashes) are stored in `tests/vectors/audit_chain.json` so an independent verifier (see ADR-0007) can be built in any language.

---

## 6. Contracts

> **SCHEMA SKETCH — not implementation.** Field names and constraints are normative; code lives in `src/airos/domain/contracts.py` in M1.

### 6.1 Task contract (Pydantic v2 sketch, `contract_version = "1.0"`)

```python
class ResourceLimits(BaseModel):
    max_tokens: int            # 1..2_000_000   default 200_000
    max_usd_micro: int         # 0..50_000_000  default 2_000_000   (2.00 USD)
    wall_clock_s: int          # 1..14_400      default 900
    container_cpus: float      # 0.25..4        default 2
    container_mem_mib: int     # 256..16_384    default 4096
    max_retries: int           # 0..5           default 2
    external_deadline_s: int | None   # only chatgpt_work; 60..259_200 default 259_200

class AcceptanceCriterion(BaseModel):
    id: str                    # unique within task, ^[a-z0-9_-]{1,40}$
    description: str
    mode: Literal["machine", "reviewer", "human"]
    check: CheckSpec | None    # required iff mode == "machine"

class CheckSpec(BaseModel):    # deterministic, re-runnable by the Verifier
    type: Literal["file_exists","sha256_equals","json_schema","regex_in_artifact",
                  "command_exit_zero","metric_threshold"]
    target: str                # logical artifact name or workspace path
    expected: JsonValue | None
    argv: list[str] | None     # only for command_exit_zero; NO shell; runs in verifier sandbox

class ApprovalRequirement(BaseModel):
    type: Literal["execute","cost","destructive","external_handoff"]
    reason: str

class ErrorRecord(BaseModel):
    ts: int; attempt: int; error_class: Literal[
        "transient","adapter_error","schema_violation","timeout","budget","policy",
        "handoff_invalid","crash_recovery","hash_mismatch","cancelled"]
    retryable: bool; detail: str   # redacted

class ExecutionRecord(BaseModel):
    run_id: str; attempt: int; agent_id: str; state: str
    started_at: int; ended_at: int | None
    request_hash: str; response_hash: str | None
    usage: ResourceUsage | None

class TaskContract(BaseModel):
    contract_version: Literal["1.0"]
    task_id: str                       # UUIDv7
    idempotency_key: str               # 16..128 chars; globally unique
    kind: Literal["work","objective"]
    research_objective: str            # 1..8_000 chars
    assigned_role: Literal["director","planner","executor","chatgpt_work","ai_scientist"]
    input_artifact_refs: list[ArtifactRef]      # {artifact_id, sha256} pinned
    expected_output: list[ExpectedOutput]       # {logical_name, media_type, artifact_class, max_bytes}
    acceptance_criteria: list[AcceptanceCriterion]   # >=1
    resource_limits: ResourceLimits
    approval_requirements: list[ApprovalRequirement]
    side_effect_class: Literal["none","workspace_write","external_write","destructive"]
    human_review_required: bool = False
    depends_on: list[str] = []
    parent_task_id: str | None
    # mutable; held on the task record, not in the hashed contract:
    status: TaskState
    error_history: list[ErrorRecord]
    execution_history: list[ExecutionRecord]
```
`contract_sha256 = sha256(canonical_json(contract))`; the contract excludes `task_id` and the mutable `status`, `error_history`, `execution_history` (those live on the task record/runs), so an idempotent re-create with a fresh `task_id` hashes identically. Validators: `dependencies acyclic`; `machine criteria have check`; `command_exit_zero` only if `assigned_role=executor`; `side_effect_class ∈ {external_write,destructive}` ⇒ `approval_requirements` contains `destructive` or `external_handoff`; `max_usd_micro > 0` ⇒ contains `cost`.

### 6.2 Agent response schema (`schema_version = "agent-response/1"`)

```python
class ProducedArtifact(BaseModel):
    logical_name: str; path: str           # relative to run output dir; no '..', no symlinks
    sha256: str                            # 64 hex
    size_bytes: int; media_type: str
    artifact_class: ArtifactClass
    parent_artifact_ids: list[str]         # subset of pinned inputs + earlier outputs

class ResourceUsage(BaseModel):
    tokens_in: int; tokens_out: int
    usd_micro: int                         # control plane RECOMPUTES from tokens × config/pricing.toml
    wall_ms: int; cpu_ms: int | None; peak_mem_bytes: int | None
    measured_by: Literal["adapter","container","self_reported"]

class AcceptanceResult(BaseModel):
    criterion_id: str
    self_reported: Literal["pass","fail","not_applicable"]
    evidence_ref: str | None               # check_id in evidence.checks

class EvidenceCheck(BaseModel):            # independently checkable
    check_id: str
    type: CheckSpec.type; target: str; expected: JsonValue | None
    reproduce: list[str] | None            # argv for the verifier; no shell

class AgentResponse(BaseModel):
    schema_version: Literal["agent-response/1"]
    task_id: str; run_id: str; attempt: int
    status: Literal["succeeded","failed","needs_input","refused"]
    retryable: bool
    produced_artifacts: list[ProducedArtifact]
    resource_usage: ResourceUsage
    acceptance_results: list[AcceptanceResult]    # one per criterion in the contract
    evidence: list[EvidenceCheck]
    notes: str                                    # free text, never trusted, redacted
    error: ErrorRecord | None
```
**Trust rules (falsifiable):** (1) `self_reported` results never satisfy a `machine` criterion — the Verifier re-executes the criterion's `check`; (2) usage with `measured_by=self_reported` is replaced by control-plane/container measurement, and a >5 % divergence emits `usage.divergence` event; (3) every `produced_artifacts[i].sha256` is recomputed from bytes; mismatch ⇒ `hash_mismatch` (non-retryable, security flag); (4) unknown `schema_version` ⇒ `schema_violation` (retryable once); (5) `acceptance_results` MUST cover exactly the contract's criterion ids.

---

## 7. Authorization

Principals: `human`, `system`, and agents by role. **Agents never authenticate to the API** (ADR-0008): adapters run inside the control plane (or a container whose only channel is files in its run directory), so an agent's authority is exactly "return an `AgentResponse`". Humans authenticate by bearer token (hash in `principals`), issued only via local CLI.

Source of truth: `config/permissions.yaml`; this table mirrors it and a test asserts equality.

### 7.1 Action × principal (✔ allowed, ✖ denied, ◐ conditional — see notes)

| Action | Human | System | Director | Planner | Executor | ChatGPT Work | AI Sci |
|---|---|---|---|---|---|---|---|
| Create root task | ✔ | ✖ | ◐1 | ✖ | ✖ | ✖ | ✖ |
| Create child tasks (draft) | ✔ | ✖ | ✖ | ◐2 | ✖ | ✖ | ✖ |
| Submit plan (T01) | ✔ | ✖ | ✖ | ◐2 | ✖ | ✖ | ✖ |
| **Grant / deny / request-changes on approvals** | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Auto-approve under policy (T04) | ✖ | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Queue (T10) / lease (T14) | ✖ | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Execute (be the run's agent) | ✖ | ✖ | ◐3 | ◐3 | ◐3 | ◐3 | ◐3 |
| Submit review verdict (T23–25) | ✔ | ✖ | ◐4 | ✖ | ✖ | ✖ | ✖ |
| Record verifier result | ✖ | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Cancel task | ✔ | ◐5 | ◐6 | ✖ | ✖ | ✖ | ✖ |
| Request budget increase | ✔ | ✔ | ✔ | ✔ | ✖ | ✖ | ✖ |
| **Grant budget / limit increase** | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Enable/disable adapter, set credentials | ✔ (host CLI) | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Release quarantined artifact | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Read full audit log / verify chain | ✔ | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Read own task's context pack | — | ✔ | ◐7 | ◐7 | ◐7 | ◐7 | ◐7 |

Notes: 1 only if `director_may_create_root=true` (default **false**). 2 only for the task it is assigned to plan, and only within T01 limits. 3 only for tasks whose `assigned_role` equals the agent's own role, only while holding that task's lease. 4 only if reviewer ≠ producer of the run under review **and** the task does not require a human reviewer (T23 guard). 5 system cancels only via cascade, timeouts, or TTL transitions. 6 only non-terminal tasks in the root objective it is assigned; for `running` it files a cancel request executed by S. 7 an adapter-built, read-only "context pack" (contract + pinned inputs); no DB access.

**Hard invariants (each has a test):** I1 `approvals.decided_by` is always a `human` principal (trigger + service). I2 No principal may transition a task it produced output for to `completed` (T23 guard) — "no agent approves its own output". I3 Budget grants require an `approval_id` whose decider is human. I4 A human token cannot be used by an adapter (adapters hold no token). I5 Agent tokens do not exist.

### 7.2 Artifact class × reader (reader = adapter acting for that role; `I` = only if the artifact is in the task's pinned `input_artifact_refs`)

| Artifact class | Human | Director | Planner | Executor | ChatGPT Work | AI Sci |
|---|---|---|---|---|---|---|
| `request` (human-authored) | ✔ | ✔ | ✔ | I | I | I |
| `plan` (Director/Planner output) | ✔ | ✔ | ✔ | I | I | ✖ |
| `work_product` (Executor output) | ✔ | ✔ (review) | I | own + I | I (+`external_handoff` approval; data leaves host) | ✖ |
| `external_handoff` (ChatGPT Work output, `untrusted`) | ✔ | ✔ (as data) | I | I (only once `reviewed`) | own | ✖ |
| `scientist_output` (`quarantined`) | ✔ (raw, host side) | ✔ only after human release | ✖ | ✖ | ✖ | own |
| `eval_result` (scores only) | ✔ | ✔ | ✔ | ✖ | ✖ | ✖ |
| **Final-evaluation data** (not an artifact class) | host only | ✖ | ✖ | ✖ | ✖ | ✖ |
| Secrets | never in any artifact; never readable by any agent through the registry |

---

## 8. Duplicate prevention and recovery

### 8.1 Duplicate prevention
- `tasks.idempotency_key` UNIQUE. `POST /v1/tasks` with an existing key and **identical** `contract_sha256` returns the existing task (HTTP 200); same key with a **different** `contract_sha256` returns 409 and records an audit event.
- `runs`: UNIQUE(task_id, attempt) and partial UNIQUE(task_id) for active states ⇒ at most one active run per task, enforced by the DB.
- Claim is a CAS on `version`; two workers (or a restarted process racing a zombie) cannot both win.
- `budget_ledger.idempotency_key` UNIQUE ⇒ a reservation/settlement cannot be applied twice.
- `approvals` partial UNIQUE ⇒ no duplicate pending/granted approvals per (task, type, version).
- **Approval invalidation (ADR-0009):** `amend_task` (any contract or resource-limit/budget change) bumps `tasks.version`, recomputes `contract_sha256`, and in the same transaction marks every `pending`/`granted` approval `invalidated`; if the task was `approved` it returns to `awaiting_approval`. `decide_approval` and transition T06/T10 re-check `(task_version, contract_sha256)` and refuse stale approvals.
- `artifacts` UNIQUE(producer_run_id, logical_name); CAS store keyed by sha256 (re-ingest is a no-op).
- Handoff dispatch deterministic path + content ⇒ re-dispatch is a no-op.

### 8.2 Lease and heartbeat
Worker identity `worker_id` = UUID generated at process start. `runs.lease_expires_at = now + 60 s`, extended every 15 s by `UPDATE runs SET heartbeat_at=?, lease_expires_at=? WHERE run_id=? AND lease_owner=? AND state IN ('leased','running')` (0 rows updated ⇒ lease lost ⇒ the worker MUST stop the agent and discard its result). The watchdog (every 5 s) handles `lease_expires_at < now` ⇒ `abandoned` ⇒ T18/T21 per §4.3. `awaiting_external` runs use `external_deadline`, not the lease.

### 8.3 Deterministic recovery on process start (in order; each step audited as `recovery.*`)
1. Acquire an exclusive `flock` on `data/airos.lock`; if held, exit non-zero (no second control plane on the same DB).
2. `PRAGMA quick_check`; WAL checkpoint; refuse to start on failure.
3. Verify audit chain tail (§5.3); mismatch ⇒ start in **read-only safe mode** (no scheduling) and alert.
4. For each `runs` row in `leased|running` whose `lease_owner ≠ my worker_id` (all of them belong to a dead process): (a) if Docker enabled, stop+remove containers labelled `airos.run_id=<id>`; (b) look for a finalised `response.json` with a completion marker in the run dir — if present and its sha256 matches `response_hash` or validates, ingest through the normal Verifier path (idempotent); (c) otherwise mark `abandoned` and settle with **worst case** (the full reservation is treated as spent; a human may later file an `adjust` after reconciling provider usage — mocks settle 0); (d) then apply T18 if `side_effect_class ∈ {none, workspace_write}` and attempts remain, else T21 `blocked(crash_recovery_decision)`.
5. `awaiting_external` runs are **not** touched; they resume polling.
6. Orphan reservations (reserve with no open run) are released with a `release` entry.
7. Sweep `approved` tasks to `queued`; emit `recovery.completed {abandoned, requeued, blocked, ingested}`.

### 8.4 Partial failure
- Ingest is one DB transaction: artifact rows, lineage, ledger settlement, state transition, audit events commit together or not at all. Files are written first (temp → fsync → rename into CAS); a crash after files but before the transaction leaves unreferenced CAS files, garbage-collected after 24 h if unreferenced.
- A crash after the agent finished but before ingest is handled by recovery step 4(b).
- A failure between container stop and DB write is covered by step 4(a)+(c).

### 8.5 Never retried automatically
Tasks with `side_effect_class ∈ {external_write, destructive}` after an unknown outcome; `policy`/security violations (network-allowlist breach, secret detected in output, mount violation, hash mismatch); `budget` errors; authorisation failures; approval denial/expiry; any `chatgpt_work` dispatch and any `ai_scientist` run (cost/duplication risk); `handoff_invalid`; human-cancelled tasks; terminal tasks (only human-created *new* tasks).

---

## 9. Approval requirements (rule R-APPROVE)
Required approvals for a task, computed deterministically at T03/T04 from its contract: `execute` if `assigned_role ∈ {executor, ai_scientist}` or `side_effect_class ≠ none`; `cost` if `max_usd_micro > auto_approve_usd_micro` (default 0 ⇒ any metered work); `destructive` if `side_effect_class = destructive`; `external_handoff` if any input artifact leaves the host (ChatGPT Work dispatch, any GitHub push). The contract's declared `approval_requirements` must be a superset of the computed set (else T01 rejects, preventing an agent from under-declaring). Approvals are bound to `(task_id, task_version, contract_sha256)`: any contract or budget change invalidates them (ADR-0009). An approval cannot be granted by the principal that created the task if that principal is an agent (agents cannot approve at all).

---

## 10. Budget enforcement

1. **Hierarchy:** `global_daily` (UTC day) ⊇ `root` ⊇ `task`; plus `ai_scientist` and optional per-`role` budgets. Proposed defaults (to confirm, Q2): global daily 25 USD, root 10 USD, task 2 USD, ai_scientist 10 USD (until a human sets otherwise, real-adapter budgets are **0**, so real adapters cannot spend).
2. **Reserve before execution (T14):** in one transaction, for each applicable budget compute `available = limit − spent − open_reservations`; require `available ≥ task.max_usd_micro` (tokens and wall-clock likewise). If any level fails ⇒ T15 `blocked(budget_insufficient)`, nothing reserved. Else append `reserve` entries at every level.
3. **Run-time enforcement:** the adapter wrapper meters every model call: before a call it computes the max cost bound (`max_tokens` × price) and refuses the call if `spent_so_far + bound > reservation`; it passes a `max_tokens` cap derived from the remaining reservation. Wall-clock is enforced by the watchdog (kill at `wall_clock_s`). Warn event at 80 %.
4. **Settle after (T17–T22):** control plane recomputes USD from measured tokens × `config/pricing.toml` (never from agent-reported USD); append `settle` (actual) and `release` (reservation − actual) entries, atomically with the transition.
5. **Hard stop:** at 100 % of the reservation the run is killed ⇒ T19/T21 (`budget`, non-retryable). If actual > reservation (an in-flight call overshoot is the only way), the ledger records the true amount, the affected budget goes `exhausted`, all `queued` tasks depending on it move to `blocked(budget_exhausted)`. Acknowledged residual risk: a single in-flight call may overshoot by at most one call's `max_tokens` bound.
6. **Increases:** only via `budget_increase` approval granted by a human; creates a `grant` ledger entry referencing `approval_id`. Agents and System may only *request*. Increase of `limit_increase` for task counts likewise.
7. **Invariant (tested after every ledger write):** `spent + open_reservations ≤ limit` for non-exhausted budgets; ledger sum of `settle+release+reserve` per run nets to `actual` once the run is terminal.

---

## 11. Safety design

### 11.1 Container policy (Executor; Scientist uses the same template with its own network/volumes)
| Control | Value |
|---|---|
| User | `--user 10001:10001` (non-root); image has no setuid binaries |
| Root FS | `--read-only`; `--tmpfs /tmp:rw,noexec,nosuid,size=256m` |
| Capabilities | `--cap-drop ALL`; no `--cap-add` |
| Privilege | `--security-opt no-new-privileges:true`; never `--privileged`; default seccomp profile (custom stricter profile later); optional gVisor `runsc` as a hardening option |
| Resources | `--cpus` and `--memory` = contract limits (defaults 2 / 4 GiB), `--memory-swap` = memory, `--pids-limit 256`, `ulimit nofile=1024` |
| Mounts | Exactly: `/work` (rw, bind of `data/workspaces/<run_id>/work`), `/inputs` (ro, copies of pinned input artifacts), `/outputs` (rw, `data/workspaces/<run_id>/out`). **No** other bind mounts; no `docker.sock`; no host `/`, home, repo, `.git`, config, secrets, or CAS root. The sandbox runner validates every mount source against an allowlist prefix `data/workspaces/<run_id>/` and a denylist (`eval-holdout`, `data/airos.db*`, `config/`, `.env*`, `/var/run/docker.sock`, `/`) before `docker run`. |
| Network | Executor attached only to user-defined bridge `exec-net` with `internal: true` (no route out) plus an egress **allowlist proxy** container on both `exec-net` and the outside; the proxy allows exactly: the Anthropic API host (default). DNS resolves only via the proxy. Package installation is disabled (frozen image, `pip --require-hashes` at image build). Network changes require a human-approved config change + audit event. |
| Commands | The control plane never builds shell strings: argv arrays only; no `sh -c`; no host shell access exposed to any agent. |
| Image | Pinned by digest recorded in `agents.image_digest`; run refuses if the digest changed without a human `adapter.enable`. |
| Cleanup | `--rm` plus label `airos.run_id`; workspace deleted or archived 7 d after terminal. |

### 11.2 Secrets
Secrets come only from environment variables or a secret store (e.g. Docker secrets/`/run/secrets`, sops-encrypted file outside the repo); never from the repo; `.gitignore` covers `.env*`, `data/`, `secrets/`. M4 decision (Q6): the Executor uses a **dedicated API key with a provider-side spend cap**, passed as a single env var to that container only. Egress-proxy key injection is deferred hardening and **not in scope** for M1–M5. All loaded secret values are registered with the `Redactor`, which (a) masks exact values (min length 8), (b) masks patterns (`sk-…`, `sk-ant-…`, `ghp_…`, `github_pat_…`, `AKIA…`, JWTs, PEM blocks), and (c) is applied to log records, event payloads, error details, captured stdout/stderr, and text artifacts at ingest — an artifact in which a secret is detected is rejected (`policy`, non-retryable) and not stored. CI runs secret scanning on every change.

### 11.3 Final-evaluation data
Stored at `/var/lib/airos-eval/holdout/` (configurable), owned by a dedicated OS user `airos-eval` mode `0700`, **outside** the repo, `data/`, and every workspace root. It is not representable in any container spec: the sandbox runner's mount validator rejects any source under that prefix, and the path is on the denylist and excluded from artifact-class resolution. Only the host-side Evaluator (a deterministic process with no LLM and no network) reads it, and it returns scores (`eval_result`) only. Test: a contract/run-spec that attempts to mount it, or a symlink resolving into it, is rejected before `docker run`.

### 11.4 Human gates and CI hygiene
- Costly or destructive operations require a human-granted approval (§9). There is no override flag.
- API binds `127.0.0.1`; behind a reverse proxy only if a human later adds TLS and authentication (out of scope for M1–M5).
- **No self-hosted GPU runner is registered for this repository**, and none is exposed to PR code from forks. CI workflows (M2+) MUST use `pull_request` (never `pull_request_target` with checkout of the PR head), `permissions: contents: read`, GitHub-hosted runners only. A CI test greps `.github/workflows/*` and fails on `self-hosted`, `pull_request_target`, or `secrets.*` in PR-triggered jobs.
- Untrusted text (handoff results, scientist output, web content) never gets tool-use authority; it is data in prompts and is labelled as such.
- **Untrusted-input rule:** the set of actions an Executor run may take (`allowed_actions`, tools, commands, paths, network hosts) is derived **only** from the approved task contract (`allowed_actions` field, bound by `contract_sha256`) and static policy. Text found in artifacts, handoff results, web pages or model output can never add, widen or select an allowed action. The `AgentRequest` builder takes the contract as its only source of `allowed_actions` (test: artifact text containing instructions leaves the request unchanged).

---

## 12. Repository directory structure (planned; created from M1 on)

```
ai-research/
├── README.md                     # project overview + how to run mocks
├── pyproject.toml                # build, ruff, mypy, pytest config (M1)
├── config/
│   ├── limits.toml               # defaults/hard caps from §4.4, §10
│   ├── permissions.yaml          # source of truth for the §7 matrix
│   ├── agents.toml               # per-role mode (mock|real), no secrets
│   └── pricing.toml              # token price table, versioned
├── docs/
│   ├── architecture/PROPOSAL.md  # this document
│   └── adr/                      # one ADR per decision
├── migrations/                   # numbered .sql (sqlite) + pg/ overrides
├── schemas/                      # generated JSON Schemas for TaskContract and AgentResponse
├── src/airos/
│   ├── domain/                   # pure logic: contracts, state machine, policy rules (no I/O)
│   ├── persistence/              # Repository Protocols + SQLite implementation, migrations runner
│   ├── services/                 # TaskService, ApprovalService, BudgetService, ArtifactRegistry, AuditLog, Recovery
│   ├── scheduler/                # worker loop, lease/heartbeat, WorkQueue Protocol + in-process impl
│   ├── adapters/                 # base Protocol, mock/, director_openai/, planner_anthropic/, executor_claude_sdk/, chatgpt_work_github/, ai_scientist/
│   ├── sandbox/                  # Docker run-spec builder, mount validator, network policy, container lifecycle
│   ├── verifier/                 # deterministic checks, hash re-computation, isolated check runner
│   ├── security/                 # authn/authz, redaction, secret loading
│   ├── api/                      # FastAPI routers, status page templates
│   └── cli/                      # `airos` admin CLI (approve, cancel, audit verify, agents enable, handoff sync)
├── containers/
│   ├── executor/                 # Dockerfile, seccomp profile, entrypoint (non-root)
│   ├── scientist/                # separate Dockerfile/compose project (never shares files with executor/)
│   └── egress-proxy/             # allowlist proxy image + config
├── tests/
│   ├── unit/                     # domain, state machine (property-based), redactor
│   ├── integration/              # SQLite repos, services, recovery with crash injection
│   ├── e2e/                      # mock flow Request→Completion
│   ├── security/                 # authz matrix, mount validator, workflow lint, secret scan
│   └── vectors/                  # audit-chain and canonical-JSON test vectors (language-neutral)
├── scripts/                      # dev helpers (no secrets, no network)
├── anchors/                      # audit head anchors (separate volume in prod; gitignored)
├── data/                         # runtime only: airos.db, CAS, workspaces (gitignored)
└── .github/workflows/            # lint+test CI (GitHub-hosted runners only)
```

---

## 13. Test plan

### 13.1 Mapping to the Phase 4 checklist

| Checklist item | Test IDs and assertions (all must pass in CI with mocks, no network, no Docker) | Milestone |
|---|---|---|
| **State persistence** | TS-01 every task/run/approval/ledger/artifact row survives close+reopen of DB. TS-02 table-driven: every (from,to) pair in §4.3 succeeds with an authorised principal and guard met; every other of the 12×12 pairs is rejected by service **and** by the DB trigger (property-based over random pairs). TS-03 terminal states reject all transitions. TS-04 CAS: two concurrent transitions on one task — exactly one wins. | M1 |
| **Process restart recovery** | TR-01..TR-08 crash injection (`kill -9` equivalents via fault hooks) at: after lease claim; mid-run; after response written/before ingest; mid-ingest transaction; after settle/before transition; during `awaiting_external`; during approval grant; during recovery itself. Assert after restart: no duplicate run for any task; ledger invariant holds; `side_effect_class ∈ {external_write,destructive}` tasks end `blocked(crash_recovery_decision)`; others re-queued or ingested; `recovery.completed` event present; audit chain valid. TR-09 second process on same DB exits non-zero (flock). | M2 |
| **Duplicate task prevention** | TD-01 same key+spec → same task id, one row. TD-02 same key, different spec → 409 + audit event. TD-03 100 concurrent identical creates → 1 row. TD-04 two workers claim same task → one run row. TD-05 replayed ledger entry rejected by UQ. TD-06 re-ingest of same response is a no-op. | M1/M2 |
| **Role authorization** | TA-01 test parses `config/permissions.yaml` and asserts it equals §7.1/§7.2 tables. TA-02 exhaustive principal×action matrix: allowed set passes, all others raise `Forbidden` and write an `authz.denied` event. TA-03 an agent role cannot grant an approval (service + DB trigger with raw SQL attempt). TA-04 producer cannot review own output (T23). TA-05 artifact read denial for each ✖ cell. TA-06 agent-held token does not exist (adapters cannot reach API without a human token). | M2 |
| **Approval blocking** | TP-01 task requiring approval never reaches `queued` without a human grant (attempt via every code path incl. scheduler sweep). TP-02 spec change after grant invalidates approval. TP-03 denied ⇒ `cancelled`; expired ⇒ `timed_out`. TP-04 grant older than 7 d ⇒ back to `awaiting_approval`. TP-05 under-declared `approval_requirements` rejected at T01. | M2 |
| **Budget enforcement** | TB-01 reservation fails ⇒ `blocked(budget_insufficient)`, nothing reserved. TB-02 settle recomputes USD from tokens, ignores agent USD. TB-03 mock adapter attempting to exceed reservation is killed ⇒ `budget` failure, no retry. TB-04 overshoot ⇒ budget `exhausted`, dependents `blocked`. TB-05 grant requires human approval; agent/system request only. TB-06 ledger invariant property test over random sequences. TB-07 concurrent reservations never over-commit. | M2 |
| **Audit logging** | TL-01 every state change has exactly one event with matching from/to in the same transaction (fault injection between them: neither persists). TL-02 UPDATE/DELETE on `task_events` aborts (SQL attempt). TL-03 verifier detects: flipped payload byte, deleted row, reordered rows, truncated tail, forged `prev_hash`. TL-04 canonical-JSON/chain test vectors match `tests/vectors/audit_chain.json`. TL-05 redacted payloads contain no registered secret value. TL-06 anchor log written at 1 000 events. | M1 |
| **Failure handling** | TF-01 each `error_class` maps to the §4.3 transition and retry rule. TF-02 retries stop at `max_retries`; backoff schedule within ±20 %. TF-03 timeout ⇒ one retry then `timed_out`. TF-04 schema-violating response ⇒ `schema_violation`, retry once. TF-05 hash mismatch / secret in output ⇒ `failed`, not retried, security flag. TF-06 depth/count limits reject plan at T01. TF-07 cascade cancel. TF-08 `fail_fast` objective. | M2 |
| **Safety (added)** | TSEC-01 mount validator rejects eval-holdout, symlinks into it, `docker.sock`, `/`. TSEC-02 container spec snapshot contains all §11.1 flags (golden file). TSEC-03 CI workflow lint (no `self-hosted`, no `pull_request_target`). TSEC-04 secret-scan clean. TSEC-05 Scientist spec shares no network/volume/env/credential names with Executor spec. TSEC-06 (M4, needs Docker) container cannot reach a non-allowlisted host, cannot write rootfs, runs as uid 10001. | M2/M4 |

### 13.2 End-to-end mock flow (E2E-01)
1. **Research Request:** human `POST /v1/tasks` (root `objective`, key K1). Assert `draft`, event #1.
2. **Planning:** mock Planner submits a plan with 3 `work` children (research via mock ChatGPT-Work drop-dir, execute via mock Executor, review). Assert T01, depth/count limits, children `planned`, dependency edges.
3. **Approval:** tasks needing approval are `awaiting_approval`; agent attempt to approve ⇒ denied; human grants ⇒ `approved ⇒ queued`.
4. **Execution:** worker leases (T14), reservation recorded, mock Executor writes artifacts + `AgentResponse`; Verifier recomputes hashes ⇒ T17 `review`; settlement recorded.
5. **Review:** mock Director returns `accept` for the Executor child (T23); for `chatgpt_work` child human review required (T23 guard) — test attempts Director accept and expects denial, then human accept.
6. **Completion:** parent objective `running→review→completed` after all children completed and human review.
7. **Post-conditions:** `airos audit verify` OK; ledger balances; every artifact has valid provenance and lineage; restart-and-verify yields identical state; all artifacts `is_mock=true`; zero network calls (socket guard fixture) and zero Docker calls.
E2E-02: same flow with kill -9 at each stage. E2E-03: failure path (executor fails twice then succeeds; once exhausts retries ⇒ `failed`, parent `failed`).

### 13.3 Quality gates (CI, from M1)
`ruff`, `mypy --strict` on `domain/` and `persistence/`, `pytest` with ≥ 90 % line coverage on `domain/`, `persistence/`, `services/`; mutation-style tests for the audit verifier (TL-03); Python 3.11 and 3.12 matrix.

---

## 14. Implementation milestones

Each milestone starts only after the human replies "approved" for that milestone (the current approval covers M1 only unless stated).

| M | Scope | Entry criteria | Exit criteria (all falsifiable) |
|---|---|---|---|
| **M1 — Domain & persistence** | `domain/` (contracts, state machine, policy), SQLite schema+migrations, repositories, audit chain + verifier, redactor, CAS skeleton, `airos` CLI skeleton | This proposal + ADRs approved; deps install approved (`pydantic`, `fastapi`, `pytest`, `ruff`, `mypy`, `hypothesis`) | TS-01..04, TD-01..03, TL-01..06 pass; coverage ≥ 90 % on domain+persistence; DB triggers reject illegal transitions and audit mutation; chain vectors file committed |
| **M2 — Control plane & mock flow** | Services (approvals, budgets, authz), scheduler/worker with leases + recovery, all five mock adapters, minimal API | M1 exit met | E2E-01..03; TR-01..09, TD-04..06, TA-01..06, TP-01..05, TB-01..07, TF-01..08, TSEC-01..05 green in CI (GitHub-hosted runner, no network, no Docker) |
| **M3 — Artifacts, verifier, status dashboard** | Full provenance/lineage, isolated check runner (subprocess, no network), read-only status page, anchors, GitHub-handoff adapter against a local fake repo | M2 exit met | Provenance recompute test passes for 100 % of artifacts in e2e; handoff ingest tests (invalid author, bad hash, oversize) pass; status page shows task/run/budget/audit status with chain verdict |
| **M4 — Sandbox & real Executor (opt-in)** | Docker sandbox runner, egress proxy, executor image, Claude Agent SDK adapter behind activation rule | M3 exit met; **a host with a Docker daemon (rootless or userns-remap) is provided by the human**; Anthropic key + budget approved by human | TSEC-06 passes on that host; Executor real-mode smoke task (human-approved, ≤ 0.50 USD) completes with settled ledger equal to measured usage ±5 %; kill -9 during container run recovers without orphan containers |
| **M5 — Real Director/Planner, GitHub handoff, AI Scientist isolation** | OpenAI/Anthropic adapters, real GitHub handoff, AI Scientist v3 container + network + volumes + budget | M4 exit met; handoff repo created; license/behaviour review of AI Scientist v3 completed by a human | TSEC-05 on real specs; Scientist cannot resolve/route to Executor network or read its volumes (negative tests on the host); quarantine→release flow audited; end-to-end real-mode dry-run costing ≤ the human-set budget |

---

## 15. Top risks, open questions and recommended defaults

### 15.1 Top 5 risks
1. **No Docker daemon here** — sandbox, egress and isolation claims are untestable until M4 on a human-supplied host; mitigated by keeping M1–M3 container-free and golden-file testing the run spec.
2. **Prompt injection / privilege escalation via untrusted artifacts** (ChatGPT Work results, Scientist output) — mitigated by `untrusted/quarantined` trust levels, no tool authority from data, human-only review for those classes, and agents having no API access.
3. **Cost overrun and unmetered spend after a crash** — mitigated by reserve-before-run, pre-call bounds, worst-case settlement on abandon, zero real-adapter budgets by default; residual: one in-flight call overshoot.
4. **Audit log tamper-evidence is only as strong as its external anchor** — a full-DB rewrite is undetectable without anchors; mitigated by the separate-volume anchor log, optional GitHub anchor, and the language-neutral verifier vectors.
5. **AI Scientist v3 executes generated code and may need broader network/GPU** — the isolation boundary could erode under pressure to "just share a volume"; mitigated by ADR-0005 negative tests (TSEC-05) and no GPU in scope.

(Also noted: SQLite single-writer contention with heartbeats — mitigated by short transactions, WAL, `busy_timeout`, and the PostgreSQL path.)

### 15.2 Open questions needing a human decision (with recommended default)

| # | Question | Recommended default |
|---|---|---|
| Q1 | Auto-approval threshold for zero-risk tasks? | `auto_approve_usd = 0` and only `side_effect_class=none`; everything metered needs a human |
| Q2 | Budget numbers (global daily / root / task / Scientist)? | 25 / 10 / 2 / 10 USD; **real-adapter budgets start at 0** until set |
| Q3 | Handoff repository for ChatGPT Work: separate repo or branch of this one? | Separate **private** repo `ai-research-handoff`; token scoped to it only |
| Q4 | Where to anchor audit head hashes? | **DECIDED:** append head hash to a file on a separate volume on every human approval and root-objective completion; human commits it weekly |
| Q5 | Docker host for M4/M5 (this sandbox has no daemon)? | Human-supplied Linux VM with rootless Docker; develop M1–M3 here |
| Q6 | Executor API credential handling | **DECIDED:** dedicated key with provider-side spend cap (M4); proxy injection deferred |
| Q7 | AI Scientist v3 source, license, network/GPU needs? | Pin to a reviewed commit digest, CPU-only, no outbound network except its single LLM host, M5 only |
| Q8 | Who reviews Director/Planner-produced outputs? | Human review mandatory (already in T23 guard) |
| Q9 | Number of human operators / is SSO needed? | Single operator now (operator token from `AIROS_OPERATOR_TOKEN`); schema supports many principals |
| Q10 | Verify GitHub commit signatures on handoff results? | Require allowlisted author **and** verified signature if the ChatGPT Work workflow can sign; otherwise allowlisted author only + mandatory human review |
| Q11 | Model IDs for each role? | Not hard-coded; set in `config/agents.toml` by the human at M4/M5 |
| Q12 | Commit these docs to `main` or a branch/PR? | Branch + PR for review; nothing committed yet |
