"""Shared helpers for real-adapter tests: fixture loading/mutation, requests, a world with fixture-backed real adapters."""
from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airos.agents.adapters import all_mock_adapters
from airos.agents.real.claude_executor import AgentLoop, LoopResult, LoopSpec
from airos.agents.real.factory import real_factories
from airos.agents.real.handoff import HandoffConfig
from airos.agents.real.transport import FixtureTransport
from airos.agents.registry import AgentRegistry, AgentSpec
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.schemas import (
    AgentRequest,
    ApprovalRequirement,
    ApprovalType,
    ExpectedOutput,
    Role,
    build_agent_request,
)
from tests.conftest import TEST_PRICES, make_contract

FIXTURES = Path(__file__).parent / "fixtures"
OPENAI_ENV, ANTHROPIC_PLANNER_ENV, ANTHROPIC_EXEC_ENV = "TEST_OPENAI_KEY", "TEST_ANTHROPIC_PLANNER_KEY", "TEST_ANTHROPIC_EXEC_KEY"
# Built at runtime so no credential-looking literal is committed.
KEY_OPENAI = "sk-" + "test-openai-" + "A1b2C3d4E5f6G7h8I9j0K1"
KEY_ANTHROPIC = "sk-ant-" + "test-planner-" + "Z9y8X7w6V5u4T3s2R1q0"
KEY_EXEC = "sk-ant-" + "test-exec-" + "Q1w2E3r4T5y6U7i8O9p0"
ENV = {OPENAI_ENV: KEY_OPENAI, ANTHROPIC_PLANNER_ENV: KEY_ANTHROPIC, ANTHROPIC_EXEC_ENV: KEY_EXEC}


def load_fixture(rel: str) -> dict[str, Any]:
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def openai_fx(*, content: Any = "[SYNTHETIC] ok", finish: str = "stop", status: int = 200, usage: Any = (10, 5),
              refusal: str | None = None, raw: str | None = None) -> dict[str, Any]:
    fx = copy.deepcopy(load_fixture("openai/chat_completions_ok.json"))
    body = fx["response"]["body"]
    body["choices"][0]["finish_reason"] = finish
    body["choices"][0]["message"]["content"] = content
    if refusal:
        body["choices"][0]["message"]["refusal"] = refusal
    if usage is None:
        del body["usage"]
    else:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    fx["response"]["status"] = status
    if raw is not None:
        fx["response"]["body"] = {"raw": raw}
    return fx


def anthropic_fx(*, text: Any = "[SYNTHETIC] plan", stop: str = "end_turn", status: int = 200, usage: Any = (20, 8),
                 blocks: Any = None, raw: str | None = None) -> dict[str, Any]:
    fx = copy.deepcopy(load_fixture("anthropic/messages_ok.json"))
    body = fx["response"]["body"]
    body["content"] = blocks if blocks is not None else [{"type": "text", "text": text}]
    body["stop_reason"] = stop
    if usage is None:
        del body["usage"]
    else:
        body["usage"] = {"input_tokens": usage[0], "output_tokens": usage[1]}
    fx["response"]["status"] = status
    if raw is not None:
        fx["response"]["body"] = {"raw": raw}
    return fx


def req(role: Role = Role.DIRECTOR, *, objective: str = "[MOCK] investigate topic X", allowed_actions: list[str] | None = None,
        outputs: list[ExpectedOutput] | None = None, inputs: list[Any] | None = None, max_tokens: int = 200_000) -> AgentRequest:
    from airos.domain.schemas import ResourceLimits

    c = make_contract(
        assigned_role=role, research_objective=objective, allowed_actions=allowed_actions or [],
        approval_requirements=[ApprovalRequirement(type=t, reason="x") for t in (ApprovalType.EXECUTE, ApprovalType.COST, ApprovalType.EXTERNAL_HANDOFF)],
        expected_output=outputs if outputs is not None else [ExpectedOutput(logical_name="out.md", media_type="text/markdown", artifact_class="plan")],
        input_artifact_refs=inputs or [], resource_limits=ResourceLimits(max_usd_micro=1_000_000, max_tokens=max_tokens))
    return build_agent_request(task_id="t-1", run_id="r-1", attempt=1, contract=c, lease_expires_at_ms=1)


class ScriptedLoop:
    """Stands in for the Claude Agent SDK loop in tests: records the spec it was given and replays a scripted result."""

    def __init__(self, result: LoopResult | None = None, writer: Any = None) -> None:
        self.specs: list[LoopSpec] = []
        self.result = result or LoopResult(final_text="done", files=[], tokens_in=50, tokens_out=10)
        self.writer = writer

    def run(self, spec: LoopSpec) -> LoopResult:
        self.specs.append(spec)
        if self.writer is not None:
            self.writer(spec)
        return self.result


assert isinstance(ScriptedLoop(), object) and AgentLoop  # (Protocol is structural; kept for readability)


# --------------------------------------------------------------------- git helpers for the handoff tests
def git(*args: str, cwd: Path, env_extra: dict[str, str] | None = None) -> str:
    import os

    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
    env.update(env_extra or {})
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True, timeout=60)  # noqa: S603,S607
    return proc.stdout.decode()


def make_bare_repo(root: Path) -> Path:
    bare = root / "exchange.git"
    bare.mkdir(parents=True)
    git("init", "--bare", "--quiet", "--initial-branch=main", str(bare), cwd=root)
    seed = root / "seed"
    seed.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=seed)
    git("config", "user.email", "seed@example.invalid", cwd=seed)
    git("config", "user.name", "seed", cwd=seed)
    (seed / "README.md").write_text("exchange repository\n")
    git("add", "README.md", cwd=seed)
    git("commit", "--quiet", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)
    return bare


def external_workflow(bare: Path, work: Path, task_id: str, *, author: str = "chatgpt-work@example.invalid",
                      files: dict[str, bytes] | None = None, result: dict[str, Any] | None = None, request_sha: str,
                      signing_key: str | None = None, gpg_home: Path | None = None) -> str:
    """Plays the external ChatGPT Work workflow: clone, write results/<task_id>/..., commit as `author`, push. Returns commit sha."""
    import hashlib

    work.mkdir(parents=True, exist_ok=True)
    clone = work / f"ext-{task_id}-{abs(hash((author, tuple(sorted((files or {}).keys())))))}"
    git("clone", "--quiet", str(bare), str(clone), cwd=work)
    git("config", "user.email", author, cwd=clone)
    git("config", "user.name", "external", cwd=clone)
    base = clone / "results" / task_id
    (base / "outputs").mkdir(parents=True, exist_ok=True)
    for name, data in (files or {}).items():
        (base / "outputs" / name).write_bytes(data)
    doc = result if result is not None else {
        "schema_version": "handoff-result/1", "task_id": task_id, "request_sha256": request_sha, "status": "succeeded",
        "outputs": [{"logical_name": n, "sha256": hashlib.sha256(d).hexdigest(), "size_bytes": len(d), "media_type": "text/markdown"}
                    for n, d in (files or {}).items()], "notes": "synthetic external result"}
    (base / "response.json").write_text(json.dumps(doc))
    git("add", "-A", cwd=clone)
    extra = {"GNUPGHOME": str(gpg_home)} if gpg_home else None
    if signing_key:
        git("-c", f"user.signingkey={signing_key}", "commit", "--quiet", "-S", "-m", "external result", cwd=clone, env_extra=extra)
    else:
        git("commit", "--quiet", "-m", "external result", cwd=clone)
    git("push", "--quiet", "origin", "HEAD:main", cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone).strip()


@dataclass
class World:
    worker: Worker
    registry: AgentRegistry
    svc: ArtifactService
    loop: ScriptedLoop
    transports: dict[str, FixtureTransport]


def real_world(repo: Any, tmp_path: Path, *, fixtures: dict[str, list[dict[str, Any]]] | None = None, env: dict[str, str] | None = None,
               handoff: HandoffConfig | None = None, loop: ScriptedLoop | None = None, roles: tuple[str, ...] = ("director", "planner", "executor", "chatgpt_work"),
               extra_specs: list[AgentSpec] | None = None) -> World:
    """Registry with REAL (fixture-backed) adapters for `roles`, mocks for the rest."""
    fixtures = fixtures or {}
    transports = {"openai": FixtureTransport(fixtures.get("openai", [])), "anthropic": FixtureTransport(fixtures.get("anthropic", []))}

    class Router:
        def send(self, request: Any) -> Any:
            return (transports["openai"] if "openai" in request.url else transports["anthropic"]).send(request)

    loop = loop or ScriptedLoop()
    specs = []
    for r in Role:
        if r.value in roles:
            cred = {"director": OPENAI_ENV, "planner": ANTHROPIC_PLANNER_ENV, "executor": ANTHROPIC_EXEC_ENV}.get(r.value)
            model = {"director": "gpt-test", "planner": "claude-test", "executor": "claude-test"}.get(r.value)
            specs.append(AgentSpec(role=r.value, kind="real", credential_env=cred, model=model))
        else:
            specs.append(AgentSpec(role=r.value))
    specs += extra_specs or []
    reg = AgentRegistry(specs, all_mock_adapters(), ENV if env is None else env, TEST_PRICES,
                        real_factories(transport=Router(), executor_loop=loop, handoff=handoff), redactor=repo.redactor)
    svc = ArtifactService(repo, ArtifactStore(tmp_path / "cas", tmp_path / "eval"))
    worker = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), svc, "w-real")
    return World(worker, reg, svc, loop, transports)


# --------------------------------------------------------------------- task builders for worker-level real-adapter tests
OUTPUT_FOR = {
    "director": ExpectedOutput(logical_name="out.md", media_type="text/markdown", artifact_class="plan"),
    "planner": ExpectedOutput(logical_name="out.md", media_type="text/markdown", artifact_class="plan"),
    "executor": ExpectedOutput(logical_name="result.md", media_type="text/markdown", artifact_class="work_product"),
    "chatgpt_work": ExpectedOutput(logical_name="notes.md", media_type="text/markdown", artifact_class="external_handoff"),
}


def role_contract(role: str, **over: Any) -> Any:
    reqs = [ApprovalRequirement(type=t, reason="x") for t in (ApprovalType.EXECUTE, ApprovalType.COST, ApprovalType.EXTERNAL_HANDOFF)]
    base: dict[str, Any] = {"assigned_role": Role(role), "approval_requirements": reqs, "expected_output": [OUTPUT_FOR[role]],
                            "allowed_actions": ["fs.read", "fs.write.workspace"] if role == "executor" else []}
    base.update(over)
    return make_contract(**base)


def grant_role_budget(repo: Any, task: Any, role: str, usd: int = 5_000_000, tokens: int = 5_000_000) -> None:
    from tests.conftest import HUMAN, PLANNER

    ap = repo.request_budget_increase(task.task_id, f"role:{role}", usd, tokens, PLANNER)
    repo.decide_approval(ap.approval_id, True, HUMAN, "grant role budget")
    repo.grant_budget(ap.approval_id)


def result_writer(spec: LoopSpec) -> None:
    (spec.cwd / "out").mkdir(exist_ok=True)
    (spec.cwd / "out" / "result.md").write_bytes(b"# executor result\n")
