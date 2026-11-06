"""SQLite implementation of the repository interfaces. Plain SQL, no ORM.

Concurrency model (ADR-0001/0002): WAL, synchronous=FULL, foreign_keys=ON, one connection per thread,
and EVERY write runs in BEGIN IMMEDIATE (single writer) with compare-and-swap on tasks.version.
Time comes only from the injected Clock. Every state change appends its audit event in the same transaction.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airos.audit import (
    GENESIS_HASH,
    AnchorVerification,
    ChainVerification,
    compute_entry_hash,
    verify_against_anchors,
    verify_rows,
)
from airos.canonical import canonical_json
from airos.clock import Clock, SystemClock
from airos.domain.guards import Facts, GuardContext, GuardInput, run_guard, run_guards
from airos.domain.limits import Limits
from airos.domain.machine import allowed_state_pairs, decide
from airos.domain.schemas import (
    Approval,
    ApprovalStatus,
    ApprovalType,
    Artifact,
    ArtifactClass,
    AuditEvent,
    BudgetLedgerEntry,
    ErrorRecord,
    LedgerKind,
    ResourceUsage,
    Role,
    Run,
    TaskContract,
    TaskKind,
    TaskRecord,
    TrustLevel,
    contract_sha256,
    provenance_hash,
    required_approval_types,
)
from airos.domain.states import (
    AGENT_KINDS,
    SIDE_EFFECTFUL,
    TERMINAL_STATES,
    Actor,
    ActorKind,
    BlockReason,
    Event,
    RunState,
    SideEffectClass,
    TaskState,
)
from airos.errors import (
    ApprovalExpired,
    BudgetInsufficient,
    GuardFailed,
    IdempotencyConflict,
    LeaseLost,
    LimitExceeded,
    NotAuthorized,
    NotFound,
    SchemaMismatch,
    SecretInContract,
    StaleApproval,
    VersionConflict,
)
from airos.persistence import budget_sql
from airos.persistence.denials import DenialAuditor
from airos.persistence.interfaces import AuditHead, CreateResult, RecoveryAction, RecoveryReport, Repository
from airos.persistence.migrate import migrate
from airos.pricing import PriceTable
from airos.redaction import Redactor
from airos.security.permissions import authorize, may_read_artifact

SYSTEM = Actor(ActorKind.SYSTEM, "system")


@dataclass(frozen=True)
class BudgetStatus:
    budget_id: str
    limit_usd_micro: int
    limit_tokens: int
    used_usd_micro: int  # outstanding reservations + settled spend
    used_tokens: int
    state: str

    @property
    def available_usd_micro(self) -> int:
        return self.limit_usd_micro - self.used_usd_micro

_DEFAULT_RUN_END: dict[Event, RunState] = {
    Event.FINISH: RunState.SUCCEEDED,
    Event.FAIL: RunState.FAILED,
    Event.TIMEOUT: RunState.TIMED_OUT,
    Event.CANCEL: RunState.CANCELLED,
    Event.CASCADE_CANCEL: RunState.CANCELLED,
}
_AMENDABLE = frozenset({TaskState.DRAFT, TaskState.PLANNED, TaskState.AWAITING_APPROVAL, TaskState.APPROVED})


class SqliteRepository:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Clock | None = None,
        redactor: Redactor | None = None,
        id_factory: Callable[[], str] | None = None,
        limits: Limits | None = None,
        anchor_path: str | os.PathLike[str] | None = None,
        real_roles: Iterable[str] = (),
        workspace_fresh_guaranteed: bool = False,
        price_table: PriceTable | None = None,
    ) -> None:
        self.path = str(path)
        self.clock: Clock = clock or SystemClock()
        self.redactor = redactor or Redactor()
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)
        self.limits = limits or Limits()
        self.anchor_path = Path(anchor_path) if anchor_path else None
        self.price_table = price_table or PriceTable.default()
        self._real_roles = frozenset(real_roles)
        self._workspace_fresh_guaranteed = workspace_fresh_guaranteed
        self._denials = DenialAuditor(self)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        conn = self._conn()
        migrate(conn, self.clock.now_ms())
        self._seed_transitions(conn)
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO principals(principal_id,type,role,display_name,created_at_ms) VALUES(?,?,?,?,?)",
                (SYSTEM.id, "system", None, "control plane", self.clock.now_ms()),
            )

    # ------------------------------------------------------------ connection / tx

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        with self._conns_lock:
            self._conns.append(c)
        return c

    def _conn(self) -> sqlite3.Connection:
        c: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
            self._local.anchors = []
        return c

    def _denial_conn(self) -> sqlite3.Connection:
        """A SECOND connection per thread, used only to audit denials after the main transaction rolled back."""
        c: sqlite3.Connection | None = getattr(self._local, "dconn", None)
        if c is None:
            c = self._connect()
            self._local.dconn = c
        return c

    @contextmanager
    def _denial_scope(self, actor: Actor, action: str, task_id: str | None = None) -> Iterator[None]:
        """Run the body; if it is denied, the main tx has already rolled back, then audit the denial separately."""
        try:
            yield
        except NotAuthorized as exc:
            self._denials.record(actor, action, task_id, str(exc))
            raise

    def set_runner_guarantee(self, fresh_dir_proven: bool) -> None:
        """Called by the worker with the RESULT of the runner's fresh-directory proof (never a constant)."""
        self._workspace_fresh_guaranteed = fresh_dir_proven

    def set_real_roles(self, roles: Iterable[str]) -> None:
        self._real_roles = frozenset(roles)

    def _effective_side_effectful(self, task: TaskRecord) -> bool:
        """external_write/destructive always. workspace_write only if fresh per-run dirs are NOT proven."""
        if task.side_effect_class in SIDE_EFFECTFUL:
            return True
        return task.side_effect_class is SideEffectClass.WORKSPACE_WRITE and not self._workspace_fresh_guaranteed
    def close(self) -> None:
        with self._conns_lock:
            for c in self._conns:
                with contextlib.suppress(sqlite3.Error):
                    c.close()
            self._conns.clear()
        self._local = threading.local()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._local.anchors = []
            raise
        self._flush_anchors()

    def _read(self) -> sqlite3.Connection:
        return self._conn()

    def _seed_transitions(self, conn: sqlite3.Connection) -> None:
        with self._tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO allowed_transitions(from_state,to_state) VALUES(?,?)",
                sorted(allowed_state_pairs()),
            )
            have = {(r[0], r[1]) for r in c.execute("SELECT from_state,to_state FROM allowed_transitions")}
        if have != allowed_state_pairs():
            raise SchemaMismatch("allowed_transitions in the database differ from the code's transition table")

    # ------------------------------------------------------------ audit

    def _append_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        actor: Actor,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        payload: dict[str, Any] | None = None,
        anchor: bool = False,
    ) -> AuditEvent:
        last = conn.execute("SELECT seq, entry_hash FROM task_events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"] + 1, last["entry_hash"]) if last else (1, GENESIS_HASH)
        clean = self.redactor.redact_json(payload or {})
        row: dict[str, Any] = {
            "seq": seq,
            "event_id": self._new_id(),
            "ts_ms": self.clock.now_ms(),
            "task_id": task_id,
            "run_id": run_id,
            "event_type": event_type,
            "actor_id": actor.id,
            "actor_kind": actor.kind.value,
            "from_state": from_state,
            "to_state": to_state,
            "payload_json": canonical_json(clean),
        }
        entry_hash = compute_entry_hash(prev, {**{k: v for k, v in row.items() if k != "payload_json"}, "payload": clean})
        conn.execute(
            "INSERT INTO task_events(seq,event_id,ts_ms,task_id,run_id,event_type,actor_id,actor_kind,"
            "from_state,to_state,payload_json,prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*row.values(), prev, entry_hash),
        )
        ev = AuditEvent(
            seq=seq, event_id=row["event_id"], ts_ms=row["ts_ms"], task_id=task_id, run_id=run_id,
            event_type=event_type, actor_id=actor.id, actor_kind=actor.kind.value,
            from_state=from_state, to_state=to_state, payload=clean, prev_hash=prev, entry_hash=entry_hash,
        )
        if anchor:
            self._local.anchors.append(ev)
        return ev

    def _flush_anchors(self) -> None:
        pending: list[AuditEvent] = self._local.anchors
        self._local.anchors = []
        if not pending or self.anchor_path is None:
            return
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.anchor_path, "a", encoding="utf-8") as f:
            for ev in pending:
                f.write(f"{ev.ts_ms}\t{ev.seq}\t{ev.entry_hash}\t{ev.event_type}\n")
            f.flush()
            os.fsync(f.fileno())

    def head(self) -> AuditHead:
        r = self._read().execute("SELECT seq, entry_hash FROM task_events ORDER BY seq DESC LIMIT 1").fetchone()
        return AuditHead(r["seq"], r["entry_hash"]) if r else AuditHead(0, GENESIS_HASH)

    def iter_events(self, from_seq: int = 1) -> Iterator[AuditEvent]:
        import json

        for r in self._read().execute("SELECT * FROM task_events WHERE seq >= ? ORDER BY seq", (from_seq,)):
            yield AuditEvent(
                seq=r["seq"], event_id=r["event_id"], ts_ms=r["ts_ms"], task_id=r["task_id"], run_id=r["run_id"],
                event_type=r["event_type"], actor_id=r["actor_id"], actor_kind=r["actor_kind"],
                from_state=r["from_state"], to_state=r["to_state"], payload=json.loads(r["payload_json"]),
                prev_hash=r["prev_hash"], entry_hash=r["entry_hash"],
            )

    def verify_chain(self) -> ChainVerification:
        rows = (dict(r) for r in self._read().execute("SELECT * FROM task_events ORDER BY seq"))
        return verify_rows(rows)

    def verify_chain_with_anchors(self) -> AnchorVerification:
        """verify_chain() plus cross-check against the anchor file (detects truncation and whole-chain rewrite)."""
        chain = self.verify_chain()
        hashes = {r["seq"]: r["entry_hash"] for r in self._read().execute("SELECT seq, entry_hash FROM task_events")}
        text = ""
        if self.anchor_path is not None and self.anchor_path.exists():
            text = self.anchor_path.read_text(encoding="utf-8")
        return verify_against_anchors(chain, hashes, text)

    # ------------------------------------------------------------ principals / authz

    def register_principal(self, principal_id: str, type: str, role: str | None, display_name: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO principals(principal_id,type,role,display_name,created_at_ms) VALUES(?,?,?,?,?)",
                (principal_id, type, role, display_name, self.clock.now_ms()),
            )
            row = conn.execute("SELECT type, role FROM principals WHERE principal_id=?", (principal_id,)).fetchone()
            if row["type"] != type or row["role"] != role:
                raise NotAuthorized(f"principal {principal_id!r} already registered with a different type/role")
            self._append_event(conn, "principal.registered", SYSTEM, payload={"principal_id": principal_id, "type": type, "role": role})

    def _check_actor(self, conn: sqlite3.Connection, actor: Actor) -> None:
        row = conn.execute("SELECT type, role, enabled FROM principals WHERE principal_id=?", (actor.id,)).fetchone()
        if row is None or not row["enabled"]:
            raise NotAuthorized(f"unknown or disabled principal {actor.id!r}")
        ok = (
            (actor.kind is ActorKind.HUMAN and row["type"] == "human")
            or (actor.kind is ActorKind.SYSTEM and row["type"] == "system")
            or (actor.kind in AGENT_KINDS and row["type"] == "agent" and row["role"] == actor.kind.value)
        )
        if not ok:
            raise NotAuthorized(f"principal {actor.id!r} is not a {actor.kind.value}")

    # ------------------------------------------------------------ tasks

    def _row_to_task(self, r: sqlite3.Row) -> TaskRecord:
        import json

        return TaskRecord(
            task_id=r["task_id"], idempotency_key=r["idempotency_key"], kind=TaskKind(r["kind"]),
            root_task_id=r["root_task_id"], parent_task_id=r["parent_task_id"], depth=r["depth"],
            assigned_role=Role(r["assigned_role"]), state=TaskState(r["state"]), version=r["version"],
            contract_version=r["contract_version"],
            blocked_reason=BlockReason(r["blocked_reason"]) if r["blocked_reason"] else None,
            blocked_from=TaskState(r["blocked_from"]) if r["blocked_from"] else None,
            contract=TaskContract.model_validate_json(r["contract_json"]), contract_sha256=r["contract_sha256"],
            side_effect_class=SideEffectClass(r["side_effect_class"]), attempt_count=r["attempt_count"],
            review_cycles=r["review_cycles"], timeout_retries=r["timeout_retries"], not_before_ms=r["not_before_ms"],
            error_history=[ErrorRecord.model_validate(e) for e in json.loads(r["error_history_json"])],
            created_by=r["created_by"], created_at_ms=r["created_at_ms"], updated_at_ms=r["updated_at_ms"],
            state_entered_at_ms=r["state_entered_at_ms"],
        )

    def _load_task(self, conn: sqlite3.Connection, task_id: str) -> TaskRecord:
        r = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if r is None:
            raise NotFound(f"task {task_id!r}")
        return self._row_to_task(r)

    def get_task(self, task_id: str) -> TaskRecord:
        return self._load_task(self._read(), task_id)

    def find_task_by_key(self, idempotency_key: str) -> TaskRecord | None:
        r = self._read().execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self._row_to_task(r) if r else None

    def list_tasks(self, state: TaskState | None = None) -> list[TaskRecord]:
        if state is None:
            q = self._read().execute("SELECT * FROM tasks ORDER BY created_at_ms, task_id")
        else:
            q = self._read().execute("SELECT * FROM tasks WHERE state=? ORDER BY created_at_ms, task_id", (state.value,))
        return [self._row_to_task(r) for r in q]

    def _assert_no_secrets(self, contract: TaskContract) -> None:
        if self.redactor.contains_secret(canonical_json(contract.model_dump(mode="json"))):
            raise SecretInContract("task contract contains a secret-looking value; refusing to store it")

    def create_task(self, contract: TaskContract, creator: Actor, parent_task_id: str | None = None) -> CreateResult:
        action = "task.create_child" if parent_task_id is not None else "task.create_root"
        if (action == "task.create_root" and creator.kind is ActorKind.DIRECTOR
                and self.limits.director_may_create_root):
            action = "task.create_root_as_director"
        with self._denial_scope(creator, action):
            return self._create_task(contract, creator, parent_task_id, action)

    def _check_inputs(self, conn: sqlite3.Connection, contract: TaskContract) -> None:
        for ref in contract.input_artifact_refs:
            row = conn.execute("SELECT sha256, artifact_class, trust_level FROM artifacts WHERE artifact_id=?",
                               (ref.artifact_id,)).fetchone()
            if row is None or row["sha256"] != ref.sha256:
                raise GuardFailed("inputs", f"input artifact {ref.artifact_id!r} is not registered with that sha256")
            if row["artifact_class"] == ArtifactClass.FINAL_EVALUATION.value:
                raise GuardFailed("inputs", "final-evaluation artifacts can never be task inputs")
            if row["trust_level"] == TrustLevel.QUARANTINED.value:
                raise GuardFailed("inputs", "quarantined artifacts must be released by a human before use")

    def _create_task(self, contract: TaskContract, creator: Actor, parent_task_id: str | None, action: str) -> CreateResult:
        self._assert_no_secrets(contract)
        chash = contract_sha256(contract)
        conflict: str | None = None
        result: CreateResult
        with self._tx() as conn:
            self._check_actor(conn, creator)
            authorize(creator.kind, action)
            existing = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (contract.idempotency_key,)).fetchone()
            if existing is not None:
                task = self._row_to_task(existing)
                if existing["contract_sha256"] == chash:
                    self._append_event(conn, "task.create_duplicate", creator, task_id=task.task_id, payload={"contract_sha256": chash})
                    result = CreateResult(task, False)
                else:
                    self._append_event(
                        conn, "task.idempotency_conflict", creator, task_id=task.task_id,
                        payload={"existing_sha256": existing["contract_sha256"], "attempted_sha256": chash},
                    )
                    conflict = contract.idempotency_key
                    result = CreateResult(task, False)
            else:
                self._check_inputs(conn, contract)
                task_id = self._new_id()
                depth, root = 0, task_id
                if parent_task_id is not None:
                    parent = self._load_task(conn, parent_task_id)
                    if parent.state in TERMINAL_STATES:
                        raise NotAuthorized("parent task is terminal")
                    depth, root = parent.depth + 1, parent.root_task_id
                    if depth > self.limits.max_subtask_depth:
                        raise LimitExceeded(f"max_subtask_depth={self.limits.max_subtask_depth}")
                    n_children = conn.execute("SELECT COUNT(*) FROM tasks WHERE parent_task_id=?", (parent_task_id,)).fetchone()[0]
                    if n_children >= self.limits.max_children_per_task:
                        raise LimitExceeded(f"max_children_per_task={self.limits.max_children_per_task}")
                    n_root = conn.execute("SELECT COUNT(*) FROM tasks WHERE root_task_id=?", (root,)).fetchone()[0]
                    if n_root >= self.limits.max_total_tasks_per_root:
                        raise LimitExceeded(f"max_total_tasks_per_root={self.limits.max_total_tasks_per_root}")
                now = self.clock.now_ms()
                conn.execute(
                    "INSERT INTO tasks(task_id,idempotency_key,kind,root_task_id,parent_task_id,depth,assigned_role,state,"
                    "version,contract_version,contract_json,contract_sha256,side_effect_class,created_by,created_at_ms,"
                    "updated_at_ms,state_entered_at_ms) VALUES(?,?,?,?,?,?,?,?,1,1,?,?,?,?,?,?,?)",
                    (task_id, contract.idempotency_key, contract.kind.value, root, parent_task_id, depth,
                     contract.assigned_role.value, TaskState.DRAFT.value,
                     canonical_json(contract.model_dump(mode="json")), chash, contract.side_effect_class.value,
                     creator.id, now, now, now),
                )
                self._append_event(
                    conn, "task.created", creator, task_id=task_id, to_state=TaskState.DRAFT.value,
                    payload={"contract_sha256": chash, "parent_task_id": parent_task_id, "depth": depth},
                )
                result = CreateResult(self._load_task(conn, task_id), True)
        if conflict is not None:
            raise IdempotencyConflict("idempotency_key re-used with a different contract")
        return result

    def add_dependency(self, task_id: str, depends_on_task_id: str, actor: Actor) -> None:
        with self._denial_scope(actor, "task.add_dependency", task_id):
            self._add_dependency(task_id, depends_on_task_id, actor)

    def _add_dependency(self, task_id: str, depends_on_task_id: str, actor: Actor) -> None:
        with self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "task.add_dependency")
            t = self._load_task(conn, task_id)
            self._load_task(conn, depends_on_task_id)
            if t.state is not TaskState.DRAFT:
                raise GuardFailed("add_dependency", "dependencies can only be added in draft")
            # cycle check: does depends_on (transitively) depend on task_id?
            seen, stack = set(), [depends_on_task_id]
            while stack:
                cur = stack.pop()
                if cur == task_id:
                    raise GuardFailed("add_dependency", "dependency cycle")
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(r[0] for r in conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (cur,)))
            conn.execute("INSERT OR IGNORE INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)", (task_id, depends_on_task_id))
            self._append_event(conn, "task.dependency_added", actor, task_id=task_id, payload={"depends_on": depends_on_task_id})

    # ------------------------------------------------------------ facts / guard input

    def _facts(self, conn: sqlite3.Connection, task: TaskRecord) -> Facts:
        now = self.clock.now_ms()
        needed = {r.type.value for r in task.contract.approval_requirements}
        granted = {
            r[0] for r in conn.execute(
                "SELECT approval_type FROM approvals WHERE task_id=? AND status='granted' AND task_version=? AND contract_sha256=?",
                (task.task_id, task.contract_version, task.contract_sha256),
            )
        }
        expired = conn.execute(
            "SELECT 1 FROM approvals WHERE task_id=? AND status='pending' AND task_version=? AND expires_at_ms<=? LIMIT 1",
            (task.task_id, task.contract_version, now),
        ).fetchone() is not None
        open_deps = conn.execute(
            "SELECT COUNT(*) FROM task_dependencies d JOIN tasks t ON t.task_id=d.depends_on_task_id "
            "WHERE d.task_id=? AND t.state<>'completed'", (task.task_id,),
        ).fetchone()[0]
        prod = conn.execute("SELECT agent_id FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task.task_id,)).fetchone()
        kids = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN state='completed' THEN 1 ELSE 0 END) FROM tasks WHERE parent_task_id=?",
            (task.task_id,),
        ).fetchone()
        n_kids, n_done = kids[0], kids[1] or 0
        return Facts(
            approvals_valid=needed <= granted, approvals_expired=expired, deps_done=open_deps == 0,
            producer_id=prod["agent_id"] if prod else None, children_all_completed=n_kids == n_done,
            has_children=n_kids > 0, ms_in_state=now - task.state_entered_at_ms, now_ms=now,
            reservation_held=budget_sql.open_task_reserve(conn, task.task_id, task.attempt_count + 1) is not None,
            side_effectful=self._effective_side_effectful(task),
        )

    def _guard_input(self, conn: sqlite3.Connection, task: TaskRecord, actor: Actor, ctx: GuardContext) -> GuardInput:
        return GuardInput(task=task, actor=actor, ctx=ctx, facts=self._facts(conn, task), limits=self.limits)

    # ------------------------------------------------------------ transitions

    def transition(
        self, task_id: str, event: Event, actor: Actor, expected_version: int,
        ctx: GuardContext | None = None, *, reason: str | None = None, error: ErrorRecord | None = None,
    ) -> TaskRecord:
        with self._denial_scope(actor, f"event:{event.value}", task_id), self._tx() as conn:
            self._check_actor(conn, actor)
            task = self._load_task(conn, task_id)
            if task.version != expected_version:
                raise VersionConflict(f"task {task_id}: expected version {expected_version}, found {task.version}")
            return self._apply(conn, task, event, actor, ctx or GuardContext(), reason=reason, error=error)
    def _apply(
        self, conn: sqlite3.Connection, task: TaskRecord, event: Event, actor: Actor, ctx: GuardContext,
        *, reason: str | None = None, error: ErrorRecord | None = None, run_end: RunState | None = None,
        usage: ResourceUsage | None = None, cause: tuple[str, str] | None = None,
    ) -> TaskRecord:
        tr = decide(task.state, event, actor.kind)  # pure table lookup + actor check (ActorNotAllowed)
        authorize(actor.kind, f"event:{event.value}")  # the permissions matrix must agree (tested)
        gi = self._guard_input(conn, task, actor, ctx)
        failed = run_guards(tr.guards, gi)
        if failed is not None:
            raise GuardFailed(failed[0], failed[1])
        now = self.clock.now_ms()
        blocked_reason = blocked_from = None
        if event is Event.BLOCK:
            assert ctx.block_reason is not None  # guaranteed by the block_reason guard
            blocked_reason, blocked_from = ctx.block_reason.value, task.state.value
        not_before = task.not_before_ms
        if event is Event.RETRY:
            not_before = ctx.not_before_ms if ctx.not_before_ms is not None else now + self.limits.backoff_ms(task.attempt_count)
        elif event is Event.START:
            not_before = None
        errors = list(task.error_history)
        clean_err: ErrorRecord | None = None
        if error is not None:
            clean_err = error.model_copy(update={"detail": self.redactor.redact_text(error.detail)})
            errors.append(clean_err)
        cur = conn.execute(
            "UPDATE tasks SET state=?, version=version+1, blocked_reason=?, blocked_from=?, not_before_ms=?, "
            "review_cycles=?, error_history_json=?, updated_at_ms=?, state_entered_at_ms=? WHERE task_id=? AND version=?",
            (tr.to_state.value, blocked_reason, blocked_from, not_before,
             task.review_cycles + (1 if event is Event.REWORK else 0),
             canonical_json([e.model_dump(mode="json") for e in errors]), now, now, task.task_id, task.version),
        )
        if cur.rowcount != 1:
            raise VersionConflict(f"task {task.task_id}: lost compare-and-swap")
        if event in (Event.REQUEST_CHANGES, Event.DENY):
            conn.execute("UPDATE approvals SET status='revoked' WHERE task_id=? AND status IN ('pending','granted')", (task.task_id,))
        elif event is Event.EXPIRE:
            conn.execute("UPDATE approvals SET status='expired' WHERE task_id=? AND status='pending' AND expires_at_ms<=?", (task.task_id, now))
        run_id: str | None = None
        if task.state is TaskState.RUNNING and task.kind is TaskKind.WORK:
            end = run_end or _DEFAULT_RUN_END.get(event, RunState.ABANDONED)
            r = conn.execute("SELECT run_id FROM runs WHERE task_id=? AND state='running'", (task.task_id,)).fetchone()
            if r is not None:
                run_id = r["run_id"]
                conn.execute(
                    "UPDATE runs SET state=?, ended_at_ms=?, error_class=?, error_detail=? WHERE run_id=?",
                    (end.value, now, clean_err.error_class if clean_err else None, clean_err.detail if clean_err else None, run_id),
                )
        if task.kind is TaskKind.WORK:
            if task.state is TaskState.RUNNING:  # settle: measured usage, or WORST CASE when unmeasured
                self._close_reservation(conn, task, task.attempt_count, run_id=run_id, settle=True, usage=usage)
            elif task.state is TaskState.QUEUED and tr.to_state is not TaskState.RUNNING:  # queued -> blocked/cancelled/...
                self._close_reservation(conn, task, task.attempt_count + 1, run_id=None, settle=False, usage=None)
        self._append_event(
            conn, "task.transition", actor, task_id=task.task_id, run_id=run_id,
            from_state=task.state.value, to_state=tr.to_state.value,
            payload={"event": event.value, "transition": tr.tid, "reason": reason, "version": task.version + 1,
                     **({"cause_task_id": cause[0], "cause_event": cause[1]} if cause else {})},
            anchor=event is Event.ACCEPT and task.parent_task_id is None,
        )
        failed_objective = event in (Event.FAIL, Event.TIMEOUT) and task.kind is TaskKind.OBJECTIVE
        if event in (Event.CANCEL, Event.CASCADE_CANCEL) or failed_objective:
            self._cascade(conn, task.task_id, cause or (task.task_id, event.value))
        return self._load_task(conn, task.task_id)

    def _cascade(self, conn: sqlite3.Connection, parent_id: str, cause: tuple[str, str]) -> list[str]:
        """SYSTEM cancels every non-terminal descendant. Running children lose their lease (their run is closed as
        cancelled) and settle at WORST CASE; queued children release their reservation; each event names the causal task."""
        authorize(SYSTEM.kind, "cascade.cancel")
        done: list[str] = []
        kids = [r[0] for r in conn.execute(
            "SELECT task_id FROM tasks WHERE parent_task_id=? AND state NOT IN ('completed','failed','timed_out','cancelled') "
            "ORDER BY created_at_ms, task_id", (parent_id,))]
        for kid in kids:
            self._apply(conn, self._load_task(conn, kid), Event.CASCADE_CANCEL, SYSTEM, GuardContext(cascade=True),
                        reason=f"ancestor {cause[0]} ended ({cause[1]})", cause=cause)
            done.append(kid)
        return done

    def cascade_cancel_descendants(self, task_id: str, *, actor: Actor = SYSTEM) -> list[str]:
        """Idempotent repair/entry point: cancel every live descendant of a task that has already ended unsuccessfully."""
        with self._denial_scope(actor, "cascade.cancel", task_id), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "cascade.cancel")
            task = self._load_task(conn, task_id)
            if task.state not in (TaskState.FAILED, TaskState.TIMED_OUT, TaskState.CANCELLED):
                raise GuardFailed("cascade", f"task is {task.state.value}; only an ended task cascades")
            return self._cascade(conn, task_id, (task_id, task.state.value))

    # ------------------------------------------------------------ budget (ledger-backed; same transaction as the transition)

    def _reserve(self, conn: sqlite3.Connection, task: TaskRecord) -> budget_sql.ReserveOutcome:
        out = budget_sql.reserve(conn, task, self.limits, self.clock.now_ms(), task.assigned_role.value in self._real_roles, self._new_id)
        if out.ok and not out.already_held:
            self._append_event(conn, "budget.reserved", SYSTEM, task_id=task.task_id, payload={
                "attempt": task.attempt_count + 1, "usd_micro": out.reserved_usd_micro, "tokens": out.reserved_tokens,
                "levels": out.levels})
        elif not out.ok:
            self._append_event(conn, "budget.reservation_refused", SYSTEM, task_id=task.task_id,
                               payload={"attempt": task.attempt_count + 1, "reason": out.reason})
        return out

    def _close_reservation(self, conn: sqlite3.Connection, task: TaskRecord, attempt: int, *, run_id: str | None,
                           settle: bool, usage: ResourceUsage | None) -> None:
        """Release the reservation; if `settle`, book the spend.

        USD is COMPUTED here as tokens x the pinned price table. Adapter-reported USD is only cross-checked (audit flag on
        >5% divergence). Unmeasured usage, a missing model, or a model without a price all settle at WORST CASE + audit flag.
        """
        actual_u: int | None = None
        actual_t: int | None = None
        if settle and usage is not None:
            cost = self.price_table.cost_usd_micro(usage.model, usage.tokens_in, usage.tokens_out)
            if cost is None:
                self._append_event(conn, "pricing.flag", SYSTEM, task_id=task.task_id, run_id=run_id, payload={
                    "reason": "missing_model" if usage.model is None else "unknown_model_no_price", "model": usage.model,
                    "price_table_version": self.price_table.version, "settled": "worst_case"})
            else:
                actual_u, actual_t = cost, usage.tokens_in + usage.tokens_out
                if abs(usage.usd_micro - cost) * 100 > 5 * max(cost, 1):
                    self._append_event(conn, "usage.divergence", SYSTEM, task_id=task.task_id, run_id=run_id, payload={
                        "adapter_reported_usd_micro": usage.usd_micro, "computed_usd_micro": cost, "model": usage.model,
                        "used": "computed"})
        n = budget_sql.close(conn, task, attempt, self.clock.now_ms(), self._new_id, run_id=run_id, settle=settle,
                             actual_usd=actual_u, actual_tokens=actual_t)
        if settle and run_id is not None and n:
            conn.execute(
                "UPDATE runs SET model=?, cost_usd_micro=?, cost_basis=?, price_table_version=?, price_table_sha256=? WHERE run_id=?",
                (usage.model if usage is not None else None, actual_u, "priced" if actual_u is not None else "worst_case",
                 self.price_table.version, self.price_table.sha256, run_id))
        if n:
            self._append_event(conn, "budget.settled" if settle else "budget.released", SYSTEM, task_id=task.task_id,
                               run_id=run_id, payload={"attempt": attempt, "levels": n, "worst_case": settle and actual_u is None,
                                                       "usd_micro": actual_u, "tokens": actual_t,
                                                       "price_table_sha256": self.price_table.sha256 if settle else None})

    def amend_task(self, task_id: str, new_contract: TaskContract, expected_version: int, actor: Actor) -> TaskRecord:
        with self._denial_scope(actor, "task.amend", task_id):
            return self._amend_task(task_id, new_contract, expected_version, actor)

    def _amend_task(self, task_id: str, new_contract: TaskContract, expected_version: int, actor: Actor) -> TaskRecord:
        self._assert_no_secrets(new_contract)
        new_hash = contract_sha256(new_contract)
        with self._tx() as conn:
            self._check_actor(conn, actor)
            task = self._load_task(conn, task_id)
            if task.version != expected_version:
                raise VersionConflict(f"task {task_id}: expected version {expected_version}, found {task.version}")
            authorize(actor.kind, "task.amend")
            if actor.kind is ActorKind.PLANNER and task.state is not TaskState.DRAFT:
                raise NotAuthorized("planner may only amend draft tasks")
            self._check_inputs(conn, new_contract)
            if task.state not in _AMENDABLE:
                raise GuardFailed("amend", f"cannot amend a task in state {task.state.value}")
            if new_contract.idempotency_key != task.idempotency_key:
                raise GuardFailed("amend", "idempotency_key is the task's identity and cannot change")
            if new_hash == task.contract_sha256:
                return task  # no-op: nothing changes, nothing is invalidated
            now = self.clock.now_ms()
            conn.execute(
                "UPDATE tasks SET contract_json=?, contract_sha256=?, contract_version=contract_version+1, "
                "version=version+1, kind=?, assigned_role=?, side_effect_class=?, updated_at_ms=? WHERE task_id=? AND version=?",
                (canonical_json(new_contract.model_dump(mode="json")), new_hash, new_contract.kind.value,
                 new_contract.assigned_role.value, new_contract.side_effect_class.value, now, task_id, task.version),
            )
            n_inval = conn.execute(
                "UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','granted')", (task_id,)
            ).rowcount
            self._append_event(
                conn, "task.amended", actor, task_id=task_id, from_state=task.state.value, to_state=task.state.value,
                payload={"old_sha256": task.contract_sha256, "new_sha256": new_hash,
                         "contract_version": task.contract_version + 1, "approvals_invalidated": n_inval},
            )
            updated = self._load_task(conn, task_id)
            if updated.state is TaskState.APPROVED and updated.contract.approval_requirements:
                updated = self._apply(conn, updated, Event.REAPPROVE, SYSTEM, GuardContext(), reason="contract amended; approvals invalidated")
            return updated

    # ------------------------------------------------------------ approvals

    def _row_to_approval(self, r: sqlite3.Row) -> Approval:
        return Approval(
            approval_id=r["approval_id"], task_id=r["task_id"], task_version=r["task_version"],
            contract_sha256=r["contract_sha256"], approval_type=ApprovalType(r["approval_type"]),
            status=ApprovalStatus(r["status"]), requested_by=r["requested_by"], requested_at_ms=r["requested_at_ms"],
            expires_at_ms=r["expires_at_ms"], decided_by=r["decided_by"], decided_at_ms=r["decided_at_ms"], reason=r["reason"],
            budget_id=r["budget_id"], amount_usd_micro=r["amount_usd_micro"], amount_tokens=r["amount_tokens"],
            worst_case_total_usd_micro=r["worst_case_usd_micro"], worst_case_total_tokens=r["worst_case_tokens"],
        )

    def get_approval(self, approval_id: str) -> Approval:
        row = self._read().execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise NotFound(f"approval {approval_id!r}")
        return self._row_to_approval(row)
    def request_approval(self, task_id: str, approval_type: ApprovalType, requested_by: Actor) -> Approval:
        with self._denial_scope(requested_by, "approval.request", task_id):
            return self._request_approval(task_id, approval_type, requested_by)

    def _request_approval(self, task_id: str, approval_type: ApprovalType, requested_by: Actor) -> Approval:
        with self._tx() as conn:
            self._check_actor(conn, requested_by)
            authorize(requested_by.kind, "approval.request")
            task = self._load_task(conn, task_id)
            if task.state not in (TaskState.PLANNED, TaskState.AWAITING_APPROVAL):
                raise GuardFailed("request_approval", f"cannot request approval in state {task.state.value}")
            if approval_type not in {r.type for r in task.contract.approval_requirements}:
                raise GuardFailed("request_approval", f"{approval_type.value} is not required by this contract")
            row = conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND approval_type=? AND task_version=? AND budget_id IS NULL AND status IN ('pending','granted')",
                (task_id, approval_type.value, task.contract_version),
            ).fetchone()
            if row is not None:
                return self._row_to_approval(row)
            now = self.clock.now_ms()
            aid = self._new_id()
            conn.execute(
                "INSERT INTO approvals(approval_id,task_id,task_version,contract_sha256,approval_type,status,requested_by,"
                "requested_at_ms,expires_at_ms,worst_case_usd_micro,worst_case_tokens) VALUES(?,?,?,?,?,'pending',?,?,?,?,?)",
                (aid, task_id, task.contract_version, task.contract_sha256, approval_type.value, requested_by.id, now,
                 now + self.limits.approval_ttl_ms, task.contract.worst_case_total_usd_micro,
                 task.contract.worst_case_total_tokens),
            )
            self._append_event(
                conn, "approval.requested", requested_by, task_id=task_id,
                payload={"approval_id": aid, "approval_type": approval_type.value, "task_version": task.contract_version,
                         "contract_sha256": task.contract_sha256},
            )
            return self._row_to_approval(conn.execute("SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone())

    def decide_approval(self, approval_id: str, grant: bool, decided_by: Actor, reason: str | None = None) -> Approval:
        with self._denial_scope(decided_by, "approval.decide"):
            return self._decide_approval(approval_id, grant, decided_by, reason)

    def _decide_approval(self, approval_id: str, grant: bool, decided_by: Actor, reason: str | None = None) -> Approval:
        deferred: Exception | None = None
        with self._tx() as conn:
            self._check_actor(conn, decided_by)
            authorize(decided_by.kind, "approval.decide")
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise NotFound(f"approval {approval_id!r}")
            ap = self._row_to_approval(row)
            task = self._load_task(conn, ap.task_id)
            now = self.clock.now_ms()
            if ap.status is not ApprovalStatus.PENDING:
                raise GuardFailed("decide_approval", f"approval is {ap.status.value}, not pending")
            if ap.task_version != task.contract_version or ap.contract_sha256 != task.contract_sha256:
                conn.execute("UPDATE approvals SET status='invalidated' WHERE approval_id=?", (approval_id,))
                self._append_event(conn, "approval.invalidated", decided_by, task_id=task.task_id,
                                   payload={"approval_id": approval_id, "reason": "stale binding"})
                deferred = StaleApproval("approval is bound to an older contract version/hash")
            elif ap.expires_at_ms <= now:
                conn.execute("UPDATE approvals SET status='expired' WHERE approval_id=?", (approval_id,))
                self._append_event(conn, "approval.expired", decided_by, task_id=task.task_id, payload={"approval_id": approval_id})
                deferred = ApprovalExpired("approval request expired")
            else:
                status = "granted" if grant else "denied"
                conn.execute(
                    "UPDATE approvals SET status=?, decided_by=?, decided_at_ms=?, reason=? WHERE approval_id=?",
                    (status, decided_by.id, now, self.redactor.redact_text(reason) if reason else None, approval_id),
                )
                self._append_event(
                    conn, f"approval.{status}", decided_by, task_id=task.task_id, anchor=True,
                    payload={"approval_id": approval_id, "approval_type": ap.approval_type.value,
                             "task_version": ap.task_version, "contract_sha256": ap.contract_sha256, "reason": reason},
                )
            out = self._row_to_approval(conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone())
        if deferred is not None:
            raise deferred
        return out

    def list_approvals(self, task_id: str) -> list[Approval]:
        return [self._row_to_approval(r) for r in self._read().execute(
            "SELECT * FROM approvals WHERE task_id=? ORDER BY requested_at_ms, approval_id", (task_id,))]

    # ------------------------------------------------------------ runs / leases

    def _row_to_run(self, r: sqlite3.Row) -> Run:
        return Run(
            run_id=r["run_id"], task_id=r["task_id"], attempt=r["attempt"], agent_id=r["agent_id"],
            state=RunState(r["state"]), lease_owner=r["lease_owner"], lease_expires_at_ms=r["lease_expires_at_ms"],
            heartbeat_at_ms=r["heartbeat_at_ms"], started_at_ms=r["started_at_ms"], ended_at_ms=r["ended_at_ms"],
            error_class=r["error_class"], error_detail=r["error_detail"], model=r["model"],
            cost_usd_micro=r["cost_usd_micro"], cost_basis=r["cost_basis"], price_table_version=r["price_table_version"],
            price_table_sha256=r["price_table_sha256"],
        )

    def list_runs(self, task_id: str) -> list[Run]:
        return [self._row_to_run(r) for r in self._read().execute("SELECT * FROM runs WHERE task_id=? ORDER BY attempt", (task_id,))]

    def start_run(self, task_id: str, worker_id: str, agent_id: str, expected_version: int, *,
                  actor: Actor = SYSTEM) -> Run:
        with self._denial_scope(actor, "run.start", task_id), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "run.start")
            task = self._load_task(conn, task_id)
            if task.version != expected_version:
                raise VersionConflict(f"task {task_id}: expected version {expected_version}, found {task.version}")
            if task.kind is not TaskKind.WORK:
                raise GuardFailed("start_run", "only work tasks have runs")
            self._check_actor(conn, Actor(ActorKind(task.assigned_role.value), agent_id))
            active = conn.execute("SELECT COUNT(*) FROM runs WHERE state='running'").fetchone()[0]
            if active >= self.limits.max_concurrent_runs:
                raise LimitExceeded(f"max_concurrent_runs={self.limits.max_concurrent_runs}")
            self._apply(conn, task, Event.START, SYSTEM, GuardContext(), reason=f"worker {worker_id}")
            now = self.clock.now_ms()
            attempt = task.attempt_count + 1
            conn.execute("UPDATE tasks SET attempt_count=? WHERE task_id=?", (attempt, task_id))
            run_id = self._new_id()
            conn.execute(
                "INSERT INTO runs(run_id,task_id,attempt,agent_id,state,lease_owner,lease_expires_at_ms,heartbeat_at_ms,started_at_ms,run_seq)"
                " VALUES(?,?,?,?,'running',?,?,?,?,(SELECT COALESCE(MAX(run_seq),0)+1 FROM runs))",
                (run_id, task_id, attempt, agent_id, worker_id, now + self.limits.lease_ttl_ms, now, now),
            )
            self._append_event(conn, "run.started", SYSTEM, task_id=task_id, run_id=run_id,
                               payload={"attempt": attempt, "agent_id": agent_id, "worker_id": worker_id,
                                        "lease_expires_at_ms": now + self.limits.lease_ttl_ms})
            return self._row_to_run(conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
    def heartbeat(self, run_id: str, worker_id: str, *, actor: Actor = SYSTEM) -> bool:
        """Extend the lease. False => lease lost (expired, taken over, or run ended): stop and discard."""
        with self._denial_scope(actor, "run.heartbeat"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "run.heartbeat")
            now = self.clock.now_ms()
            cur = conn.execute(
                "UPDATE runs SET heartbeat_at_ms=?, lease_expires_at_ms=? "
                "WHERE run_id=? AND lease_owner=? AND state='running' AND lease_expires_at_ms>?",
                (now, now + self.limits.lease_ttl_ms, run_id, worker_id, now),
            )
            return cur.rowcount == 1
    def complete_run(
        self, run_id: str, worker_id: str, outcome: RunState, *, retryable: bool = False,
        verifier_passed: bool = False, error: ErrorRecord | None = None, usage: ResourceUsage | None = None,
        actor: Actor = SYSTEM,
    ) -> TaskRecord:
        if outcome not in (RunState.SUCCEEDED, RunState.FAILED, RunState.TIMED_OUT):
            raise ValueError("outcome must be succeeded, failed or timed_out")
        with self._denial_scope(actor, "run.complete"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "run.complete")
            now = self.clock.now_ms()
            r = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if r is None:
                raise NotFound(f"run {run_id!r}")
            if r["lease_owner"] != worker_id or r["state"] != "running" or r["lease_expires_at_ms"] <= now:
                raise LeaseLost(f"run {run_id}: lease not held by {worker_id!r}; discard the result")
            task = self._load_task(conn, r["task_id"])
            if task.state is not TaskState.RUNNING:
                raise LeaseLost(f"task {task.task_id} is no longer running")
            if now >= self._run_deadline_ms(r, task):  # the hard limit is the control plane's, not the adapter's
                return self._time_out_run_row(conn, task, r)  # a result delivered at/after the deadline is discarded
            if outcome is RunState.SUCCEEDED:  # settles inside _apply
                return self._apply(conn, task, Event.FINISH, SYSTEM, GuardContext(verifier_passed=verifier_passed),
                                   reason="run succeeded", error=error, run_end=RunState.SUCCEEDED, usage=usage)
            # failure: settle FIRST so the retry reservation sees the freed budget
            self._close_reservation(conn, task, task.attempt_count, run_id=run_id, settle=True, usage=usage)
            ctx = GuardContext(error_retryable=retryable)
            end = RunState.TIMED_OUT if outcome is RunState.TIMED_OUT else RunState.FAILED
            timeout_ok = outcome is not RunState.TIMED_OUT or task.timeout_retries < self.limits.max_timeout_retries
            if timeout_ok and run_guard("retry_allowed", self._guard_input(conn, task, SYSTEM, ctx)) is None:
                res = self._reserve(conn, task)
                if res.ok:
                    out = self._apply(conn, task, Event.RETRY, SYSTEM, ctx, reason="retryable failure", error=error, run_end=end)
                    if outcome is RunState.TIMED_OUT:
                        conn.execute("UPDATE tasks SET timeout_retries=timeout_retries+1 WHERE task_id=?", (task.task_id,))
                        out = self._load_task(conn, task.task_id)
                    return out
                return self._apply(conn, task, Event.BLOCK, SYSTEM, GuardContext(block_reason=BlockReason.BUDGET_INSUFFICIENT),
                                   reason=f"retry blocked: {res.reason}", error=error, run_end=end)
            ev = Event.TIMEOUT if outcome is RunState.TIMED_OUT else Event.FAIL
            return self._apply(conn, task, ev, SYSTEM, ctx, reason="non-retryable failure", error=error, run_end=end)
    # ------------------------------------------------------------ wall-clock timeouts (enforced here, never by the adapter)

    @staticmethod
    def _run_deadline_ms(run_row: sqlite3.Row, task: TaskRecord) -> int:
        return int(run_row["started_at_ms"]) + task.contract.resource_limits.wall_clock_s * 1000

    def _time_out_run_row(self, conn: sqlite3.Connection, task: TaskRecord, run_row: sqlite3.Row) -> TaskRecord:
        """The run ends as timed_out; the reservation settles at WORST CASE; normal retry rules apply, but at most
        `max_timeout_retries` timeouts are ever retried."""
        now = self.clock.now_ms()
        limit_s = task.contract.resource_limits.wall_clock_s
        err = ErrorRecord(ts_ms=now, attempt=task.attempt_count, error_class="timeout", retryable=True,
                          detail=f"wall-clock limit of {limit_s}s exceeded")
        self._close_reservation(conn, task, task.attempt_count, run_id=run_row["run_id"], settle=True, usage=None)
        ctx = GuardContext(error_retryable=True)
        if task.timeout_retries < self.limits.max_timeout_retries and \
                run_guard("retry_allowed", self._guard_input(conn, task, SYSTEM, ctx)) is None:
            res = self._reserve(conn, task)
            if not res.ok:
                return self._apply(conn, task, Event.BLOCK, SYSTEM, GuardContext(block_reason=BlockReason.BUDGET_INSUFFICIENT),
                                   reason=f"timeout retry blocked: {res.reason}", error=err, run_end=RunState.TIMED_OUT)
            self._apply(conn, task, Event.RETRY, SYSTEM, ctx, reason="wall-clock timeout; timeout retry", error=err,
                        run_end=RunState.TIMED_OUT)
            conn.execute("UPDATE tasks SET timeout_retries=timeout_retries+1 WHERE task_id=?", (task.task_id,))
            return self._load_task(conn, task.task_id)
        return self._apply(conn, task, Event.TIMEOUT, SYSTEM, ctx, reason="wall-clock timeout", error=err, run_end=RunState.TIMED_OUT)

    def time_out_run(self, run_id: str, *, actor: Actor = SYSTEM) -> TaskRecord:
        """Expire one run whose wall-clock limit is reached (elapsed >= wall_clock_s). Refused before that."""
        with self._denial_scope(actor, "run.timeout"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "run.timeout")
            r = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if r is None:
                raise NotFound(f"run {run_id!r}")
            if r["state"] != "running":
                raise GuardFailed("time_out_run", f"run is {r['state']}, not running")
            task = self._load_task(conn, r["task_id"])
            if self.clock.now_ms() < self._run_deadline_ms(r, task):
                raise GuardFailed("time_out_run", "wall-clock limit not reached")
            return self._time_out_run_row(conn, task, r)

    def run_deadline_passed(self, run_id: str) -> bool:
        """True if the run is still `running` but its wall-clock limit is reached (read-only)."""
        r = self._read().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if r is None or r["state"] != "running":
            return False
        return self.clock.now_ms() >= self._run_deadline_ms(r, self._load_task(self._read(), r["task_id"]))

    def time_out_expired_runs(self, *, actor: Actor = SYSTEM) -> list[str]:
        """Sweep every running run (any worker, any adapter) past its deadline, in run_seq order."""
        ids = [x[0] for x in self._read().execute("SELECT run_id FROM runs WHERE state='running' ORDER BY run_seq")]
        done: list[str] = []
        for rid in ids:
            try:
                self.time_out_run(rid, actor=actor)
                done.append(rid)
            except GuardFailed:
                continue
        return done

    def recover_leases(self, current_worker_id: str | None = None, *, actor: Actor = SYSTEM) -> RecoveryReport:
        """Deterministic recovery (ordered by run_seq) of runs whose lease expired, or whose owner is not `current_worker_id`.

        The abandoned run settles at WORST CASE. Side-effectful tasks are NEVER re-run automatically: they go to
        blocked(crash_recovery_decision). Other tasks are requeued only if a fresh reservation for the next attempt succeeds.
        """
        with self._denial_scope(actor, "run.recover"):
            self._check_actor(self._read(), actor)
            authorize(actor.kind, "run.recover")
        now = self.clock.now_ms()
        ids = [r[0] for r in self._read().execute(
            "SELECT run_id FROM runs WHERE state='running' AND (lease_expires_at_ms<=? OR (? IS NOT NULL AND lease_owner<>?)) "
            "ORDER BY run_seq", (now, current_worker_id, current_worker_id))]
        actions: list[RecoveryAction] = []
        for run_id in ids:
            with self._tx() as conn:
                r = conn.execute("SELECT * FROM runs WHERE run_id=? AND state='running'", (run_id,)).fetchone()
                if r is None:
                    continue
                now = self.clock.now_ms()
                task = self._load_task(conn, r["task_id"])
                side = self._effective_side_effectful(task)
                err = ErrorRecord(ts_ms=now, attempt=task.attempt_count, error_class="crash_recovery", retryable=not side,
                                  detail=f"lease of worker {r['lease_owner']} expired or owner is dead")
                if task.state is not TaskState.RUNNING:
                    conn.execute("UPDATE runs SET state='abandoned', ended_at_ms=? WHERE run_id=?", (now, run_id))
                    continue
                self._close_reservation(conn, task, task.attempt_count, run_id=run_id, settle=True, usage=None)  # worst case
                if side:
                    self._apply(conn, task, Event.BLOCK, SYSTEM, GuardContext(block_reason=BlockReason.CRASH_RECOVERY_DECISION),
                                reason="side-effectful task: human decision required", error=err, run_end=RunState.ABANDONED)
                    act = RecoveryAction(run_id, task.task_id, "blocked", "side-effectful; never auto-rerun")
                else:
                    ctx = GuardContext(error_retryable=True)
                    if run_guard("retry_allowed", self._guard_input(conn, task, SYSTEM, ctx)) is None:
                        res = self._reserve(conn, task)
                        if res.ok:
                            self._apply(conn, task, Event.RETRY, SYSTEM, ctx, reason="lease expired; requeued", error=err,
                                        run_end=RunState.ABANDONED)
                            act = RecoveryAction(run_id, task.task_id, "requeued", "retry allowed")
                        else:
                            self._apply(conn, task, Event.BLOCK, SYSTEM,
                                        GuardContext(block_reason=BlockReason.BUDGET_INSUFFICIENT),
                                        reason=f"requeue blocked: {res.reason}", error=err, run_end=RunState.ABANDONED)
                            act = RecoveryAction(run_id, task.task_id, "blocked", f"budget: {res.reason}")
                    else:
                        self._apply(conn, task, Event.FAIL, SYSTEM, ctx, reason="lease expired; retries exhausted", error=err,
                                    run_end=RunState.ABANDONED)
                        act = RecoveryAction(run_id, task.task_id, "failed", "retries exhausted")
                actions.append(act)
        with self._tx() as conn:
            self._append_event(conn, "recovery.completed", SYSTEM, payload={
                "abandoned": len(actions), "requeued": sum(a.action == "requeued" for a in actions),
                "blocked": sum(a.action == "blocked" for a in actions), "failed": sum(a.action == "failed" for a in actions)})
        return RecoveryReport(actions)
    # ------------------------------------------------------------ artifacts / ledger (minimal in M1)

    def add_artifact(self, artifact: Artifact, actor: Actor) -> Artifact:
        """Register an artifact. Trust level and class/role pairing are ENFORCED here, not trusted from the caller."""
        with self._denial_scope(actor, "artifact.register"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "artifact.register")
            cls, trust = artifact.artifact_class, artifact.trust_level
            for pid in artifact.parent_artifact_ids:
                if conn.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (pid,)).fetchone() is None:
                    raise NotFound(f"parent artifact {pid!r} must be registered first")
            if artifact.producer_run_id is not None:
                r = conn.execute("SELECT t.assigned_role, t.contract_json FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                                 "WHERE r.run_id=?", (artifact.producer_run_id,)).fetchone()
                if r is None:
                    raise NotFound(f"producer run {artifact.producer_run_id!r}")
                role = r["assigned_role"]
                want = {"chatgpt_work": ArtifactClass.EXTERNAL_HANDOFF, "ai_scientist": ArtifactClass.SCIENTIST_OUTPUT}.get(role)
                if want is not None and cls is not want:
                    raise GuardFailed("provenance", f"{role} output must be class {want.value}")
                if want is None and cls in (ArtifactClass.EXTERNAL_HANDOFF, ArtifactClass.SCIENTIST_OUTPUT, ArtifactClass.FINAL_EVALUATION):
                    raise GuardFailed("provenance", f"{role} may not produce class {cls.value}")
                contract = TaskContract.model_validate_json(r["contract_json"])
                allowed = {ref.artifact_id for ref in contract.input_artifact_refs}
                allowed |= {x[0] for x in conn.execute("SELECT artifact_id FROM artifacts WHERE producer_run_id=?", (artifact.producer_run_id,))}
                extra = set(artifact.parent_artifact_ids) - allowed
                if extra:
                    raise GuardFailed("provenance", f"parents {sorted(extra)} are neither pinned inputs nor outputs of this run")
            # Untrusted by construction: whatever the caller claims, these classes are never stored as trusted.
            if cls is ArtifactClass.EXTERNAL_HANDOFF and trust not in (TrustLevel.UNTRUSTED, TrustLevel.QUARANTINED):
                trust = TrustLevel.UNTRUSTED
            if cls is ArtifactClass.SCIENTIST_OUTPUT:
                trust = TrustLevel.QUARANTINED
            stored = artifact.model_copy(update={"provenance_hash": provenance_hash(artifact), "trust_level": trust})
            conn.execute(
                "INSERT INTO artifacts(artifact_id,sha256,size_bytes,media_type,artifact_class,logical_name,storage_uri,"
                "producer_run_id,producer_principal_id,trust_level,is_mock,provenance_hash,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stored.artifact_id, stored.sha256, stored.size_bytes, stored.media_type, stored.artifact_class.value,
                 stored.logical_name, stored.storage_uri, stored.producer_run_id, stored.producer_principal_id,
                 stored.trust_level.value, int(stored.is_mock), stored.provenance_hash, stored.created_at_ms),
            )
            for pid in stored.parent_artifact_ids:
                conn.execute("INSERT INTO artifact_lineage(child_artifact_id,parent_artifact_id,relation) VALUES(?,?,'derived_from')",
                             (stored.artifact_id, pid))
            self._append_event(conn, "artifact.registered", actor, run_id=stored.producer_run_id,
                               payload={"artifact_id": stored.artifact_id, "sha256": stored.sha256, "class": cls.value,
                                        "trust_level": trust.value, "provenance_hash": stored.provenance_hash,
                                        "parents": sorted(stored.parent_artifact_ids)})
            return stored

    def authorize_artifact_read(self, actor: Actor, artifact_id: str, for_task_id: str | None = None) -> Artifact:
        """Decide (from the permissions matrix) whether `actor` may read the artifact's bytes. Denials are audited."""
        art = self.get_artifact(artifact_id)
        action = f"artifact.read:{art.artifact_class.value}"
        with self._denial_scope(actor, action, for_task_id):
            conn = self._read()
            self._check_actor(conn, actor)
            pinned = False
            if for_task_id is not None:
                task = self._load_task(conn, for_task_id)
                pinned = artifact_id in {ref.artifact_id for ref in task.contract.input_artifact_refs}
            own = False
            if art.producer_run_id is not None:
                r = conn.execute("SELECT agent_id FROM runs WHERE run_id=?", (art.producer_run_id,)).fetchone()
                own = r is not None and r["agent_id"] == actor.id
            if not may_read_artifact(actor.kind, art.artifact_class.value, pinned_or_own=pinned or own,
                                     quarantined=art.trust_level is TrustLevel.QUARANTINED):
                raise NotAuthorized(f"{actor.kind.value} may not read {art.artifact_class.value} artifact {artifact_id}")
        return art

    def release_artifact(self, artifact_id: str, actor: Actor) -> Artifact:
        """Human releases a quarantined artifact to `untrusted` (never directly to trusted)."""
        with self._denial_scope(actor, "artifact.release"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "artifact.release")
            cur = conn.execute("UPDATE artifacts SET trust_level='untrusted' WHERE artifact_id=? AND trust_level='quarantined'",
                               (artifact_id,))
            if cur.rowcount != 1:
                raise GuardFailed("release_artifact", "artifact is not quarantined")
            self._append_event(conn, "artifact.released", actor, payload={"artifact_id": artifact_id, "to": "untrusted"})
        return self.get_artifact(artifact_id)

    def record_system_event(self, event_type: str, payload: dict[str, Any], task_id: str | None = None,
                            actor: Actor = SYSTEM) -> AuditEvent:
        """Audited errors raised outside a state change (e.g. routing failures). Event types are allow-listed."""
        if event_type not in {"agent.route_failed", "worker.error"}:
            raise ValueError(f"event type {event_type!r} is not allowed here")
        with self._denial_scope(actor, "audit.append_system_event", task_id), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "audit.append_system_event")
            return self._append_event(conn, event_type, actor, task_id=task_id, payload=payload)
    def get_artifact(self, artifact_id: str) -> Artifact:
        r = self._read().execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if r is None:
            raise NotFound(f"artifact {artifact_id!r}")
        parents = [x[0] for x in self._read().execute(
            "SELECT parent_artifact_id FROM artifact_lineage WHERE child_artifact_id=? ORDER BY parent_artifact_id", (artifact_id,))]
        return Artifact(
            artifact_id=r["artifact_id"], sha256=r["sha256"], size_bytes=r["size_bytes"], media_type=r["media_type"],
            artifact_class=r["artifact_class"], logical_name=r["logical_name"], storage_uri=r["storage_uri"],
            producer_run_id=r["producer_run_id"], producer_principal_id=r["producer_principal_id"],
            trust_level=r["trust_level"], is_mock=bool(r["is_mock"]), parent_artifact_ids=parents,
            created_at_ms=r["created_at_ms"], provenance_hash=r["provenance_hash"],
        )

    # ------------------------------------------------------------ budget API (human gates, queueing, status)

    def reserve_budget(self, task_id: str, expected_version: int, *, actor: Actor = SYSTEM) -> budget_sql.ReserveOutcome:
        """Explicit reservation in its own transaction. Prefer queue_task()/enqueue(), which reserve AND transition atomically."""
        with self._denial_scope(actor, "budget.reserve", task_id), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "budget.reserve")
            task = self._load_task(conn, task_id)
            if task.version != expected_version:
                raise VersionConflict(f"task {task_id}: expected version {expected_version}, found {task.version}")
            if task.state not in (TaskState.APPROVED, TaskState.BLOCKED, TaskState.REVIEW):
                raise GuardFailed("reserve_budget", f"cannot reserve in state {task.state.value}")
            return self._reserve(conn, task)

    def enqueue(self, task_id: str, event: Event, actor: Actor, expected_version: int,
                ctx: GuardContext | None = None, *, reason: str | None = None) -> TaskRecord:
        """QUEUE / UNBLOCK / REWORK: reserve budget and transition to queued in ONE BEGIN IMMEDIATE transaction.

        The transition's own guard re-reads the ledger and refuses unless an open reservation row exists.
        QUEUE with insufficient budget parks the task in blocked(budget_insufficient); UNBLOCK/REWORK raise BudgetInsufficient.
        """
        if event not in (Event.QUEUE, Event.UNBLOCK, Event.REWORK):
            raise ValueError("enqueue handles QUEUE, UNBLOCK and REWORK only")
        with self._denial_scope(actor, f"event:{event.value}", task_id), self._tx() as conn:
            self._check_actor(conn, actor)
            task = self._load_task(conn, task_id)
            if task.version != expected_version:
                raise VersionConflict(f"task {task_id}: expected version {expected_version}, found {task.version}")
            decide(task.state, event, actor.kind)  # illegal/unauthorised events never touch the ledger
            if task.kind is TaskKind.WORK:
                res = self._reserve(conn, task)
                if not res.ok:
                    if event is Event.QUEUE:
                        return self._apply(conn, task, Event.BLOCK, SYSTEM,
                                           GuardContext(block_reason=BlockReason.BUDGET_INSUFFICIENT),
                                           reason=f"queue refused: {res.reason}")
                    raise BudgetInsufficient(res.reason or "insufficient budget")
            return self._apply(conn, task, event, actor, ctx or GuardContext(), reason=reason)

    def queue_task(self, task_id: str, expected_version: int, *, actor: Actor = SYSTEM) -> TaskRecord:
        return self.enqueue(task_id, Event.QUEUE, actor, expected_version)

    def set_budget(self, budget_id: str, scope: str, scope_ref: str, usd_micro: int, tokens: int, actor: Actor) -> None:
        """Human sets an INITIAL base limit. Base limits are immutable afterwards; increases need approval + grant_budget()."""
        with self._denial_scope(actor, "budget.set"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "budget.set")
            if conn.execute("SELECT 1 FROM budgets WHERE budget_id=?", (budget_id,)).fetchone() is not None:
                raise GuardFailed("set_budget", "budget already exists; increases require a human-approved grant")
            budget_sql.ensure_budget(conn, budget_id, scope, scope_ref, usd_micro, tokens, actor.id, self.clock.now_ms())
            self._append_event(conn, "budget.set", actor, payload={"budget_id": budget_id, "usd_micro": usd_micro, "tokens": tokens})

    def request_budget_increase(self, task_id: str, budget_id: str, usd_micro: int, tokens: int, requested_by: Actor) -> Approval:
        """File a budget_increase approval bound to the task's CURRENT contract_version + contract_sha256."""
        with self._denial_scope(requested_by, "budget.request_increase", task_id), self._tx() as conn:
            self._check_actor(conn, requested_by)
            authorize(requested_by.kind, "budget.request_increase")
            task = self._load_task(conn, task_id)
            if task.state in TERMINAL_STATES:
                raise GuardFailed("budget_increase", "task is terminal")
            if usd_micro < 0 or tokens < 0 or (usd_micro == 0 and tokens == 0):
                raise GuardFailed("budget_increase", "amount must be positive")
            if budget_id.split(":", 1)[0] not in ("task", "root", "global", "role"):
                raise GuardFailed("budget_increase", f"unknown budget {budget_id!r}")
            row = conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND approval_type='budget_increase' AND task_version=? AND budget_id=? "
                "AND status IN ('pending','granted')", (task_id, task.contract_version, budget_id)).fetchone()
            if row is not None:
                return self._row_to_approval(row)
            now = self.clock.now_ms()
            aid = self._new_id()
            conn.execute(
                "INSERT INTO approvals(approval_id,task_id,task_version,contract_sha256,approval_type,status,requested_by,"
                "requested_at_ms,expires_at_ms,budget_id,amount_usd_micro,amount_tokens,worst_case_usd_micro,worst_case_tokens)"
                " VALUES(?,?,?,?,'budget_increase','pending',?,?,?,?,?,?,?,?)",
                (aid, task_id, task.contract_version, task.contract_sha256, requested_by.id, now,
                 now + self.limits.approval_ttl_ms, budget_id, usd_micro, tokens, task.contract.worst_case_total_usd_micro,
                 task.contract.worst_case_total_tokens))
            self._append_event(conn, "approval.requested", requested_by, task_id=task_id, payload={
                "approval_id": aid, "approval_type": "budget_increase", "budget_id": budget_id, "usd_micro": usd_micro,
                "tokens": tokens, "task_version": task.contract_version, "contract_sha256": task.contract_sha256})
            return self._row_to_approval(conn.execute("SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone())

    def grant_budget(self, approval_id: str, *, actor: Actor = SYSTEM) -> BudgetLedgerEntry:
        """Apply a HUMAN-granted budget_increase approval to the ledger. Single use; refused if the contract changed since."""
        deferred: Exception | None = None
        with self._denial_scope(actor, "budget.grant"), self._tx() as conn:
            self._check_actor(conn, actor)
            authorize(actor.kind, "budget.grant")
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise NotFound(f"approval {approval_id!r}")
            if row["approval_type"] != "budget_increase":
                raise GuardFailed("grant_budget", "not a budget_increase approval")
            dup = conn.execute("SELECT * FROM budget_ledger WHERE idempotency_key=?", (f"grant:{approval_id}",)).fetchone()
            if dup is not None:
                return self._row_to_ledger(dup)  # single use: replay returns the same entry, never a second grant
            human = conn.execute("SELECT 1 FROM principals WHERE principal_id=? AND type='human' AND enabled=1",
                                 (row["decided_by"],)).fetchone()
            if row["status"] != "granted" or human is None:
                raise NotAuthorized("budget grant requires an approval granted by a human principal")
            task = self._load_task(conn, row["task_id"])
            if row["task_version"] != task.contract_version or row["contract_sha256"] != task.contract_sha256:
                conn.execute("UPDATE approvals SET status='invalidated' WHERE approval_id=?", (approval_id,))
                self._append_event(conn, "approval.invalidated", actor, task_id=task.task_id,
                                   payload={"approval_id": approval_id, "reason": "stale binding at grant time"})
                deferred = StaleApproval("budget approval is bound to an older contract version/hash")
            else:
                bid = row["budget_id"]
                kind, _, ref = bid.partition(":")
                now = self.clock.now_ms()
                if kind == "root":
                    budget_sql.ensure_budget(conn, bid, "root", ref, self.limits.default_root_usd_micro, self.limits.default_root_tokens, "system", now)
                elif kind == "global":
                    budget_sql.ensure_budget(conn, bid, "global_daily", ref, self.limits.default_global_daily_usd_micro, self.limits.default_global_daily_tokens, "system", now)
                elif kind == "role":
                    budget_sql.ensure_budget(conn, bid, "role", ref, self.limits.real_adapter_default_usd_micro, self.limits.real_adapter_default_tokens, "system", now)
                entry_id = self._new_id()
                conn.execute(
                    "INSERT INTO budget_ledger(entry_id,budget_id,task_id,run_id,kind,usd_micro,tokens,wall_s,approval_id,"
                    "idempotency_key,created_at_ms,attempt,reserve_ref) VALUES(?,?,?,NULL,'grant',?,?,0,?,?,?,NULL,NULL)",
                    (entry_id, bid, task.task_id, row["amount_usd_micro"], row["amount_tokens"], approval_id,
                     f"grant:{approval_id}", now))
                budget_sql.reactivate_if_within_limit(conn, bid)
                self._append_event(conn, "budget.granted", actor, task_id=task.task_id, payload={
                    "approval_id": approval_id, "budget_id": bid, "usd_micro": row["amount_usd_micro"],
                    "tokens": row["amount_tokens"], "decided_by": row["decided_by"]})
            out = conn.execute("SELECT * FROM budget_ledger WHERE idempotency_key=?", (f"grant:{approval_id}",)).fetchone()
        if deferred is not None:
            raise deferred
        return self._row_to_ledger(out)

    def budget_status(self, budget_id: str) -> BudgetStatus:
        conn = self._read()
        kind, _, ref = budget_id.partition(":")
        if kind == "task":
            t = self._load_task(conn, ref)
            gu, gt = budget_sql.grants(conn, budget_id)
            lim_u, lim_t = budget_sql.task_limits(t, gu, gt)
            state = "active"
        else:
            row = conn.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
            if row is None:
                raise NotFound(f"budget {budget_id!r}")
            lim_u, lim_t = budget_sql.effective_limits(conn, row)
            state = row["state"]
        u, t_used = budget_sql.usage(conn, budget_id)
        return BudgetStatus(budget_id, lim_u, lim_t, u, t_used, state)


    @staticmethod
    def _row_to_ledger(r: sqlite3.Row) -> BudgetLedgerEntry:
        return BudgetLedgerEntry(
            entry_id=r["entry_id"], budget_id=r["budget_id"], task_id=r["task_id"], run_id=r["run_id"],
            kind=LedgerKind(r["kind"]), usd_micro=r["usd_micro"], tokens=r["tokens"], wall_s=r["wall_s"],
            approval_id=r["approval_id"], attempt=r["attempt"], reserve_ref=r["reserve_ref"],
            idempotency_key=r["idempotency_key"], created_at_ms=r["created_at_ms"],
        )

    def list_ledger(self, budget_id: str | None = None) -> list[BudgetLedgerEntry]:
        if budget_id is None:
            q = self._read().execute("SELECT * FROM budget_ledger ORDER BY created_at_ms, entry_id")
        else:
            q = self._read().execute("SELECT * FROM budget_ledger WHERE budget_id=? ORDER BY created_at_ms, entry_id", (budget_id,))
        return [self._row_to_ledger(r) for r in q]


def _conforms_to_repository(r: SqliteRepository) -> Repository:
    """Static check (mypy --strict): SqliteRepository structurally implements every repository Protocol."""
    return r


__all__ = ["SYSTEM", "SqliteRepository", "required_approval_types"]
