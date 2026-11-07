"""In-process scheduler/worker (ADR-0002). One `tick()` = queue approved tasks, start queued ones, run them via mock adapters.

Everything that changes state goes through the repository (guards, ledger, audit). The worker holds no authority of its own.
"""
from __future__ import annotations

import contextlib
import threading
import time
import uuid
from dataclasses import dataclass, field

from pydantic import ValidationError

from airos.agents.real.common import AdapterProtocolError
from airos.agents.registry import AgentRegistry
from airos.agents.runner import Runner
from airos.artifacts import ArtifactService
from airos.domain.guards import GuardContext
from airos.domain.schemas import (
    AgentResponse,
    ErrorRecord,
    ResourceUsage,
    TaskKind,
    TaskRecord,
    build_agent_request,
)
from airos.domain.states import Actor, ActorKind, BlockReason, Event, RunState, TaskState
from airos.errors import (
    AdapterUnavailable,
    AirosError,
    GuardFailed,
    IntegrityError,
    LeaseLost,
    LimitExceeded,
    NotAuthorized,
    UnsafePath,
    VersionConflict,
)
from airos.persistence.interfaces import RecoveryReport
from airos.persistence.sqlite_repo import SYSTEM, SqliteRepository
from airos.verifier import verify_response

MAX_RESPONSE_BYTES = 1 * 1024 * 1024


class AdapterTimeout(AirosError):
    """The adapter did not return within the contract's wall-clock limit."""


@dataclass
class TickReport:
    queued: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    blocked_unroutable: list[str] = field(default_factory=list)
    finished_objectives: list[str] = field(default_factory=list)
    failed_objectives: list[str] = field(default_factory=list)


class Worker:
    def __init__(self, repo: SqliteRepository, registry: AgentRegistry, runner: Runner, artifacts: ArtifactService,
                 worker_id: str) -> None:
        self.repo, self.registry, self.runner, self.artifacts, self.worker_id = repo, registry, runner, artifacts, worker_id
        repo.set_real_roles(registry.real_roles())
        # The side-effect rule for workspace_write tasks depends on a PROVEN property of the runner, not a declared one.
        repo.set_runner_guarantee(self.prove_fresh_run_dirs(runner))

    @staticmethod
    def prove_fresh_run_dirs(runner: Runner) -> bool:
        """Black-box proof (the runner's own claims are ignored): two run ids give two DISTINCT directories, a dirty
        directory never leaks into the next one, and re-preparing an existing run id is refused."""
        tag = uuid.uuid4().hex
        try:
            a = runner.prepare(f"proof-{tag}", f"proof-{tag}-a")
            (a / "dirty").write_text("leftover")
            b = runner.prepare(f"proof-{tag}", f"proof-{tag}-b")
            if a.resolve() == b.resolve() or any(b.iterdir()):
                return False
            try:
                again = runner.prepare(f"proof-{tag}", f"proof-{tag}-a")
            except FileExistsError:
                return True
            return again.resolve() != a.resolve() and not any(again.iterdir())
        except Exception:  # noqa: BLE001 - any failure to prove means NOT proven
            return False

    def start(self) -> RecoveryReport:
        """Startup: recover every run owned by a dead process (deterministic, run_seq order)."""
        return self.repo.recover_leases(self.worker_id)

    def sweep_timeouts(self) -> list[str]:
        """TTL watchdog (approval request, review, blocked). Each transition is guarded by the TTL itself, so calling
        this early is harmless. Returns ids moved to timed_out."""
        moved: list[str] = []
        for t in self.repo.list_tasks(TaskState.APPROVED):  # human grants older than 7 days no longer authorise execution
            try:
                self.repo.transition(t.task_id, Event.REAPPROVE, SYSTEM, t.version, reason="approval grant expired")
            except (GuardFailed, VersionConflict):
                continue
        for state, event in ((TaskState.AWAITING_APPROVAL, Event.EXPIRE), (TaskState.REVIEW, Event.REVIEW_TIMEOUT),
                             (TaskState.BLOCKED, Event.BLOCK_TIMEOUT)):
            for t in self.repo.list_tasks(state):
                try:
                    self.repo.transition(t.task_id, event, SYSTEM, t.version, reason="ttl expired")
                    moved.append(t.task_id)
                except (GuardFailed, VersionConflict):
                    continue
        return moved

    def tick(self) -> TickReport:
        rep = TickReport()
        self.sweep_timeouts()
        self.repo.time_out_expired_runs()  # hard per-run limit, independent of any adapter or worker
        for t in self.repo.list_tasks(TaskState.APPROVED):
            if t.kind is TaskKind.WORK:
                try:
                    self.registry.route(self.repo, t)  # availability first: audited refusal BEFORE any budget is touched
                except AdapterUnavailable as exc:
                    self.repo.transition(t.task_id, Event.BLOCK, SYSTEM, t.version,
                                         GuardContext(block_reason=BlockReason.ADAPTER_UNAVAILABLE), reason=str(exc))
                    rep.blocked_unroutable.append(t.task_id)
                    continue
            try:
                out = self.repo.queue_task(t.task_id, t.version)
            except (GuardFailed, VersionConflict):
                continue  # e.g. dependencies not complete yet
            (rep.queued if out.state is TaskState.QUEUED else rep.blocked_unroutable).append(t.task_id)
        for t in self.repo.list_tasks(TaskState.QUEUED):
            if t.kind is TaskKind.OBJECTIVE:
                self.repo.transition(t.task_id, Event.START, SYSTEM, t.version)
                continue
            if t.not_before_ms is not None and t.not_before_ms > self.repo.clock.now_ms():
                continue
            try:
                adapter = self.registry.route(self.repo, t)
            except AdapterUnavailable as exc:
                self.repo.transition(t.task_id, Event.BLOCK, SYSTEM, t.version,
                                     GuardContext(block_reason=BlockReason.ADAPTER_UNAVAILABLE), reason=str(exc))
                rep.blocked_unroutable.append(t.task_id)
                continue
            try:
                run = self.repo.start_run(t.task_id, self.worker_id, f"{t.assigned_role.value}-1", t.version)
            except LimitExceeded:
                break
            rep.started.append(t.task_id)
            self._execute(self.repo.get_task(t.task_id), run.run_id, run.attempt, run.lease_expires_at_ms, adapter)
        all_tasks = self.repo.list_tasks()
        for t in self.repo.list_tasks(TaskState.RUNNING):
            if t.kind is TaskKind.OBJECTIVE:
                kids = [k for k in all_tasks if k.parent_task_id == t.task_id]
                if any(k.state in (TaskState.FAILED, TaskState.TIMED_OUT, TaskState.CANCELLED) for k in kids):
                    self.repo.transition(t.task_id, Event.FAIL, SYSTEM, t.version, reason="fail_fast: a child did not complete")
                    rep.failed_objectives.append(t.task_id)
                    continue
                try:
                    self.repo.transition(t.task_id, Event.FINISH, SYSTEM, t.version)
                    rep.finished_objectives.append(t.task_id)
                except GuardFailed:
                    pass  # children still in flight
        return rep

    heartbeat_interval_s = 15.0

    def _join(self, thread: threading.Thread, timeout_s: float, run_id: str) -> None:
        """Wait up to timeout_s (real time) for the adapter thread, heartbeating the lease while it works.
        Seam for tests, which must not sleep. Returns early if the lease is lost (recovery then owns the run)."""
        end = time.monotonic() + timeout_s
        while thread.is_alive():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            thread.join(min(remaining, self.heartbeat_interval_s))
            if thread.is_alive() and not self.repo.heartbeat(run_id, self.worker_id):
                return

    def _call_adapter(self, adapter, request, workdir, timeout_s: int, inputs=None):  # type: ignore[no-untyped-def]
        """Run the adapter in a daemon thread so a hung adapter cannot hang the worker past the wall-clock limit."""
        box: dict[str, object] = {}

        def target() -> None:
            try:
                box["response"] = adapter.execute(request, workdir) if adapter.is_mock else adapter.execute(request, workdir, inputs)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        th = threading.Thread(target=target, daemon=True, name=f"adapter-{request.run_id[:8]}")
        th.start()
        self._join(th, timeout_s, request.run_id)
        if th.is_alive():
            raise AdapterTimeout(f"adapter still running after {timeout_s}s")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["response"]

    def _audit(self, event_type: str, task: TaskRecord, payload: dict[str, object]) -> None:
        with contextlib.suppress(AirosError):
            self.repo.record_system_event(event_type, payload, task.task_id)

    def _reject(self, task: TaskRecord, run_id: str, category: str, detail: str, usage: ResourceUsage | None, adapter) -> None:  # type: ignore[no-untyped-def]
        """Fail closed on a bad adapter response and AUDIT it. Malformed / missing-artifact / schema failures are retried once
        per task (PROPOSAL); hash mismatches, oversize, policy violations and handoff problems are never retried."""
        self._audit("adapter.response_rejected", task, {"role": task.assigned_role.value, "category": category,
                                                        "detail": detail[:1000], "mock": adapter.is_mock, "run_id": run_id})
        error_class = {"hash_mismatch": "hash_mismatch", "handoff_invalid": "handoff_invalid", "malformed": "schema_violation",
                       "missing_artifact": "schema_violation", "schema": "schema_violation"}.get(category, "policy")
        prior = sum(e.error_class == "schema_violation" for e in task.error_history)
        retryable = error_class == "schema_violation" and prior == 0
        self._fail(run_id, retryable=retryable, error_class=error_class, detail=f"{category}: {detail}", usage=usage)

    def _fail(self, run_id: str, *, retryable: bool, error_class: str, detail: str, usage: ResourceUsage | None = None) -> None:
        err = ErrorRecord(ts_ms=self.repo.clock.now_ms(), attempt=0, error_class=error_class,
                          retryable=retryable, detail=detail)
        with contextlib.suppress(LeaseLost):  # lease lost: the result is discarded, recovery owns this run now
            self.repo.complete_run(run_id, self.worker_id, RunState.FAILED, retryable=retryable, error=err, usage=usage)

    def _execute(self, task: TaskRecord, run_id: str, attempt: int, lease_expires: int, adapter) -> None:  # type: ignore[no-untyped-def]
        workdir = self.runner.prepare(task.task_id, run_id)
        request = build_agent_request(task_id=task.task_id, run_id=run_id, attempt=attempt, contract=task.contract,
                                      lease_expires_at_ms=lease_expires)
        if not self.repo.heartbeat(run_id, self.worker_id):
            return
        agent = Actor(ActorKind(task.assigned_role.value), f"{task.assigned_role.value}-1")

        def read_input(artifact_id: str) -> bytes:  # the adapter can read ONLY what this agent role may read (pinned inputs)
            return self.artifacts.read(agent, artifact_id, for_task_id=task.task_id)

        try:
            response = self._call_adapter(adapter, request, workdir, task.contract.resource_limits.wall_clock_s, read_input)
        except AdapterTimeout:
            # Hard limit enforced by the control plane: the adapter cannot extend, ignore or outlive it.
            with contextlib.suppress(GuardFailed, LeaseLost):
                self.repo.time_out_run(run_id)
            return
        except AdapterProtocolError as exc:
            self._reject(task, run_id, exc.category, exc.detail, None, adapter)
            return
        except NotAuthorized as exc:  # the adapter tried to read something its role may not read: policy, never retried
            self._reject(task, run_id, "policy", f"permission denied while running: {exc}", None, adapter)
            return
        except ValidationError as exc:  # the adapter tried to build an invalid AgentResponse: malformed output
            self._reject(task, run_id, "malformed", f"invalid adapter output ({exc.error_count()} validation error(s))", None, adapter)
            return
        except Exception as exc:  # adapter crash: transient, retried within limits
            self._fail(run_id, retryable=True, error_class="adapter_error", detail=f"adapter raised {type(exc).__name__}: {exc}")
            return
        if self.repo.run_deadline_passed(run_id):  # late result: discard BEFORE anything is stored
            with contextlib.suppress(GuardFailed, LeaseLost):
                self.repo.time_out_run(run_id)
            return
        if not isinstance(response, AgentResponse):  # anything that is not a valid AgentResponse is malformed: fail closed
            try:
                response = AgentResponse.model_validate(response)
            except ValidationError:
                self._reject(task, run_id, "malformed", "adapter output does not validate against agent-response/1", None, adapter)
                return
        if len(response.model_dump_json()) > MAX_RESPONSE_BYTES:
            self._reject(task, run_id, "oversized", "serialized adapter response is too large", None, adapter)
            return
        usage = response.resource_usage
        if not adapter.is_mock:
            self._audit("adapter.response_received", task, {"role": task.assigned_role.value, "status": response.status,
                                                            "artifacts": len(response.produced_artifacts), "notes": response.notes[:500]})
        if response.status == "needs_input":
            with contextlib.suppress(LeaseLost):
                self.repo.park_run(run_id, self.worker_id, response.notes or "needs external input", usage=usage)
            return
        if response.status != "succeeded":
            self._fail(run_id, retryable=response.retryable, error_class=(response.error.error_class if response.error else "adapter_error"),
                       detail=(response.error.detail if response.error else f"status {response.status}"), usage=usage)
            return
        forbidden = (self.artifacts.store.root, self.artifacts.store.eval_root)
        verdict = verify_response(request, response, workdir, forbidden)
        if not verdict.passed:
            joined = "; ".join(verdict.problems)
            if any("hash_mismatch" in p for p in verdict.problems):
                category = "hash_mismatch"
            elif any("oversized" in p for p in verdict.problems):
                category = "oversized"
            elif any("does not exist" in p or "missing expected outputs" in p for p in verdict.problems):
                category = "missing_artifact"
            else:
                category = "schema"
            self._reject(task, run_id, category, joined, usage, adapter)
            return
        try:
            for p in response.produced_artifacts:
                self.artifacts.ingest(run_dir=workdir, produced=p, run_id=run_id, task=task, artifact_id=uuid.uuid4().hex,
                                      is_mock=adapter.is_mock)
        except (IntegrityError, UnsafePath) as exc:
            self._fail(run_id, retryable=False, error_class="hash_mismatch", detail=str(exc), usage=usage)
            return
        except AirosError as exc:
            self._fail(run_id, retryable=False, error_class="policy", detail=str(exc), usage=usage)
            return
        with contextlib.suppress(LeaseLost):
            self.repo.complete_run(run_id, self.worker_id, RunState.SUCCEEDED, verifier_passed=True, usage=usage)
