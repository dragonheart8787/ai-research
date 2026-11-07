"""Real-clock integration test for the lease heartbeat. Every other lease test uses FakeClock and never sleeps; this one uses the SYSTEM clock,
a short lease, and a worker/adapter that really take longer than the lease. It sleeps ~1.7 s per case on purpose (the point is real elapsed time)."""
from __future__ import annotations

import threading
import time

from airos.agents.adapters import MockAdapter, all_mock_adapters
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.clock import SystemClock
from airos.domain.limits import Limits
from airos.domain.schemas import ExpectedOutput, ResourceLimits, Role
from airos.domain.states import TaskState
from tests.conftest import HUMAN, approve_fully, make_contract
from tests.test_adapter_response_validation import _ok

LEASE_MS = 600
WORK_S = 1.5                       # > 2 x the lease: without heartbeats the lease MUST expire
OUT = [ExpectedOutput(logical_name="o.txt", media_type="text/plain", artifact_class="work_product", max_bytes=100)]


class SlowAdapter(MockAdapter):
    def __init__(self) -> None:
        super().__init__(Role.EXECUTOR)

    def execute(self, request, workdir, inputs=None):
        time.sleep(WORK_S)                                           # real time
        return _ok(request, workdir)


def _world(open_repo, tmp_path, interval_s: float):
    repo = open_repo(clock=SystemClock(), limits=Limits(lease_ttl_ms=LEASE_MS))
    adapters = all_mock_adapters()
    adapters[Role.EXECUTOR] = SlowAdapter()
    reg = AgentRegistry([AgentSpec(role=r.value) for r in Role], adapters, {})
    w = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), ArtifactService(repo, ArtifactStore(tmp_path / "c", tmp_path / "e")), "w-real")
    w.heartbeat_interval_s = interval_s
    t = approve_fully(repo, repo.create_task(make_contract(expected_output=OUT, resource_limits=ResourceLimits(
        max_usd_micro=1_000_000, max_retries=0, wall_clock_s=30)), HUMAN).task)
    return repo, w, t


def _watch(repo, stop: threading.Event, seen: list):
    """Another process's recovery sweep, run repeatedly while the adapter works."""
    while not stop.is_set():
        seen.extend(repo.recover_leases().actions)
        stop.wait(0.15)


def test_a_slow_adapter_keeps_its_lease_because_the_worker_really_heartbeats(open_repo, tmp_path):
    repo, w, t = _world(open_repo, tmp_path, interval_s=0.1)
    stop, seen = threading.Event(), []
    th = threading.Thread(target=_watch, args=(repo, stop, seen), daemon=True)
    th.start()
    start = time.monotonic()
    w.tick()
    elapsed = time.monotonic() - start
    stop.set()
    th.join(5)
    assert elapsed >= WORK_S - 0.05 and elapsed > 2 * LEASE_MS / 1000        # it really outlived two leases
    assert seen == []                                                          # recovery never found an expired lease
    assert repo.get_task(t.task_id).state is TaskState.REVIEW
    [run] = repo.list_runs(t.task_id)
    assert run.state.value == "succeeded"
    hb = repo._conn().execute("SELECT heartbeat_at_ms - started_at_ms FROM runs WHERE run_id=?", (run.run_id,)).fetchone()[0]
    assert hb >= 2 * LEASE_MS                                                  # the lease was extended well past its original TTL


def test_control_without_heartbeats_the_same_run_loses_its_lease_and_is_recovered(open_repo, tmp_path):
    repo, w, t = _world(open_repo, tmp_path, interval_s=30.0)                  # effectively no heartbeat during the run
    stop, seen = threading.Event(), []
    th = threading.Thread(target=_watch, args=(repo, stop, seen), daemon=True)
    th.start()
    w.tick()
    stop.set()
    th.join(5)
    assert [a.action for a in seen] and seen[0].task_id == t.task_id           # recovery DID take the run
    assert repo.get_task(t.task_id).state is not TaskState.REVIEW              # and the late result was discarded
    assert [r.state.value for r in repo.list_runs(t.task_id)] == ["abandoned"]


assert LEASE_MS > 0 and WORK_S > 2 * LEASE_MS / 1000   # the test's own premise: the work outlives two leases
