"""Mock-mode demo: one task from creation to human acceptance using ONLY mock adapters. No credentials, no network, no containers.

    .venv/bin/python scripts/mock_demo.py [DATA_DIR]      # default: a fresh temporary directory

It builds the registry from the mock table (every real adapter is disabled by default and is not even constructed), runs one task through
approval -> queue -> run -> review -> human accept, then verifies the audit chain. Mock output is placeholder data, not research.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))   # the locked environment does not install the package itself

from airos.agents.adapters import all_mock_adapters  # noqa: E402
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.guards import GuardContext
from airos.domain.schemas import (
    AcceptanceCriterion,
    ApprovalRequirement,
    ApprovalType,
    ArtifactClass,
    ExpectedOutput,
    ResourceLimits,
    Role,
    TaskContract,
)
from airos.domain.states import Actor, ActorKind, Event
from airos.persistence.sqlite_repo import SYSTEM, SqliteRepository

HUMAN = Actor(ActorKind.HUMAN, "demo-human")
PLANNER = Actor(ActorKind.PLANNER, "demo-planner")


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(tempfile.mkdtemp(prefix="airos-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(root / "airos.db", anchor_path=root / "anchors" / "audit-anchors.log")
    for actor, kind, role in ((HUMAN, "human", None), (PLANNER, "agent", "planner"), (Actor(ActorKind.EXECUTOR, "executor-1"), "agent", "executor")):
        repo.register_principal(actor.id, kind, role, actor.id)
    registry = AgentRegistry([AgentSpec(role=r.value) for r in Role], all_mock_adapters(), {})       # kind="mock" for every role
    worker = Worker(repo, registry, FreshDirRunner(root / "workspaces"), ArtifactService(repo, ArtifactStore(root / "cas", root / "eval")),
                    "demo-worker", host_environ={})
    contract = TaskContract(
        idempotency_key="demo-0001-mock-task", research_objective="[MOCK] demonstrate the control plane", assigned_role=Role.EXECUTOR,
        acceptance_criteria=[AcceptanceCriterion(id="c1", description="has output", mode="reviewer")],
        resource_limits=ResourceLimits(max_usd_micro=0, max_retries=0),
        approval_requirements=[ApprovalRequirement(type=ApprovalType.EXECUTE, reason="demo")],
        allowed_actions=["fs.read", "fs.write.workspace"],
        expected_output=[ExpectedOutput(logical_name="result.md", media_type="text/markdown", artifact_class=ArtifactClass.WORK_PRODUCT)])
    task = repo.create_task(contract, HUMAN).task
    t = repo.transition(task.task_id, Event.PLAN, PLANNER, task.version, GuardContext(plan_ok=True))
    t = repo.transition(t.task_id, Event.REQUIRE_APPROVAL, SYSTEM, t.version)
    for req in t.contract.approval_requirements:
        repo.decide_approval(repo.request_approval(t.task_id, req.type, SYSTEM).approval_id, True, HUMAN, "demo approval")
    t = repo.transition(t.task_id, Event.APPROVE, HUMAN, t.version)
    repo.queue_task(t.task_id, t.version)
    worker.tick()
    t = repo.get_task(t.task_id)
    print(f"after run: state={t.state.value}")
    t = repo.transition(t.task_id, Event.ACCEPT, HUMAN, t.version)
    print(f"after human accept: state={t.state.value}")
    verdict = repo.verify_chain_with_anchors()
    print(f"audit chain ok={verdict.ok} data_dir={root}")
    repo.close()
    return 0 if verdict.ok and t.state.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
