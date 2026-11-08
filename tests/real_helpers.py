"""Shared helpers for real-adapter tests: CLI fixtures (SYNTHETIC; see tests/fixtures/README.md), requests, and a world with
fixture-backed real adapters. Nothing here runs a provider CLI; the FixtureCliRunner replays text."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from airos.agents.adapters import all_mock_adapters
from airos.agents.real.claude_executor import LoopResult, LoopSpec
from airos.agents.real.cli import FixtureCliRunner
from airos.agents.real.factory import real_factories
from airos.agents.real.handoff import HandoffConfig
from airos.agents.registry import AgentRegistry, AgentSpec, RegistryPolicy
from airos.agents.runner import FreshDirRunner
from airos.agents.worker import Worker
from airos.artifacts import ArtifactService, ArtifactStore
from airos.domain.schemas import (
    AgentRequest,
    ApprovalRequirement,
    ApprovalType,
    ExpectedOutput,
    ResourceLimits,
    Role,
    build_agent_request,
)
from tests.conftest import TEST_PRICES, make_contract

FIXTURES = Path(__file__).parent / "fixtures"
PLANNER_ENV, EXECUTOR_ENV = "AIROS_CLAUDE_TOKEN_PLANNER", "AIROS_CLAUDE_TOKEN_EXECUTOR"
# Built at runtime so no credential-looking literal is committed.
TOKEN_PLANNER = "sk-ant-" + "oat01-planner-" + "A1b2C3d4E5f6G7h8I9j0K1"
TOKEN_EXECUTOR = "sk-ant-" + "oat01-executor-" + "Q1w2E3r4T5y6U7i8O9p0"
ENV = {PLANNER_ENV: TOKEN_PLANNER, EXECUTOR_ENV: TOKEN_EXECUTOR}
POLICY = RegistryPolicy(usage_credits_attested_off=True, attested_on=date(2026, 11, 1), today=lambda: date(2026, 11, 7))
DIRECTOR_MODEL = "director-test-model"
FORBIDDEN_HOST_ENV = {
    "ANTHROPIC_API_KEY": "fake-anthropic-api-key-value", "ANTHROPIC_AUTH_TOKEN": "fake-anthropic-auth-token", "OPENAI_API_KEY": "fake-openai-api-key",
    "CODEX_API_KEY": "fake-codex-api-key", "GEMINI_API_KEY": "fake-gemini-api-key", "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKEFAKE",
    "AWS_SECRET_ACCESS_KEY": "fake-aws-secret-access-key", "AWS_SESSION_TOKEN": "fake-aws-session-token",
    "AWS_BEARER_TOKEN_BEDROCK": "fake-bedrock-bearer", "CLAUDE_CODE_USE_BEDROCK": "1"}


def plan_json(n: int = 2) -> str:
    return json.dumps({"schema_version": "plan-proposal/1", "tasks": [
        {"title": f"step {i}", "objective": f"do step {i}", "role": "executor", "depends_on": [i - 1] if i else []} for i in range(n)]})


def claude_fx(result: Any = None, *, returncode: int = 0, auth: str | None = "oauth", usage: Any = (20, 8), is_error: bool = False,
              subtype: str = "success", stderr: str = "", stdout_raw: str | None = None, stop_reason: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "result", "subtype": subtype, "is_error": is_error,
                            "result": plan_json() if result is None else result}
    if auth is not None:
        body["apiKeySource"] = auth
    if usage is not None:
        body["usage"] = {"input_tokens": usage[0], "output_tokens": usage[1]}
    if stop_reason:
        body["stop_reason"] = stop_reason
    return {"name": "claude -p (synthetic)", "provenance": "synthetic: written from the documented CLI behaviour; NOT a live recording",
            "match": {"argv_prefix": ["claude", "-p"]},
            "result": {"returncode": returncode, "stdout": stdout_raw if stdout_raw is not None else json.dumps(body), "stderr": stderr}}


def codex_status_fx(text: str = "Logged in using ChatGPT", returncode: int = 0) -> dict[str, Any]:
    return {"name": "codex login status (synthetic)", "provenance": "synthetic: NOT a live recording",
            "match": {"argv_prefix": ["codex", "login", "status"]}, "result": {"returncode": returncode, "stdout": text, "stderr": ""}}


def codex_exec_fx(message: Any = "[SYNTHETIC] director brief", *, usage: Any = (120, 30), returncode: int = 0, stderr: str = "",
                  extra_lines: list[str] | None = None, error: str | None = None) -> dict[str, Any]:
    lines = [json.dumps({"type": "thread.started", "thread_id": "t-synthetic"})]
    if message is not None:
        lines.append(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": message}}))
    if error:
        lines.append(json.dumps({"type": "error", "message": error}))
    if usage is not None:
        lines.append(json.dumps({"type": "turn.completed", "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}))
    lines += extra_lines or []
    return {"name": "codex exec (synthetic)", "provenance": "synthetic: NOT a live recording",
            "match": {"argv_prefix": ["codex", "exec"]}, "result": {"returncode": returncode, "stdout": "\n".join(lines), "stderr": stderr}}


def req(role: Role = Role.DIRECTOR, *, objective: str = "[MOCK] investigate topic X", allowed_actions: list[str] | None = None,
        outputs: list[ExpectedOutput] | None = None, inputs: list[Any] | None = None, max_tokens: int = 200_000) -> AgentRequest:
    c = make_contract(
        assigned_role=role, research_objective=objective, allowed_actions=allowed_actions or [],
        approval_requirements=[ApprovalRequirement(type=t, reason="x") for t in (ApprovalType.EXECUTE, ApprovalType.COST, ApprovalType.EXTERNAL_HANDOFF)],
        expected_output=outputs if outputs is not None else [ExpectedOutput(logical_name="out.md", media_type="text/markdown", artifact_class="plan")],
        input_artifact_refs=inputs or [], resource_limits=ResourceLimits(max_usd_micro=1_000_000, max_tokens=max_tokens))
    return build_agent_request(task_id="t-1", run_id="r-1", attempt=1, contract=c, lease_expires_at_ms=1)


class ScriptedLoop:
    """Stands in for the Claude Agent SDK sandbox loop in tests: records the spec it was given and replays a scripted result.
    kind='fixture': it executes nothing."""

    kind = "fixture"

    def __init__(self, result: LoopResult | None = None, writer: Any = None) -> None:
        self.specs: list[LoopSpec] = []
        self.result = result or LoopResult(final_text="done", files=[], tokens_in=50, tokens_out=10, auth_source="oauth")
        self.writer = writer

    def run(self, spec: LoopSpec) -> LoopResult:
        self.specs.append(spec)
        if self.writer is not None:
            self.writer(spec)
        return self.result


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
    runner: FixtureCliRunner


def real_world(repo: Any, tmp_path: Path, *, fixtures: list[dict[str, Any]] | None = None, env: dict[str, str] | None = None,
               handoff: HandoffConfig | None = None, loop: ScriptedLoop | None = None,
               roles: tuple[str, ...] = ("director", "planner", "executor", "chatgpt_work"), extra_specs: list[AgentSpec] | None = None,
               policy: RegistryPolicy | None = None, host_environ: dict[str, str] | None = None) -> World:
    """Registry with REAL (fixture-backed, sandbox-kind) adapters for `roles`, mocks for the rest. Everything the human must attest is attested."""
    runner = FixtureCliRunner(fixtures or [])
    loop = loop or ScriptedLoop()
    specs = []
    for r in Role:
        if r.value in roles:
            cred = {"planner": PLANNER_ENV, "executor": EXECUTOR_ENV}.get(r.value)
            model = {"director": DIRECTOR_MODEL, "planner": "claude-opus-5-5", "executor": "claude-sonnet-5-5"}.get(r.value)
            opts = {"no_api_key_attested": "1"} if r.value == "director" else {}
            mode = "manual_handoff" if r.value == "chatgpt_work" else "subscription_oauth"
            specs.append(AgentSpec(role=r.value, kind="real", credential_env=cred, model=model, options=opts, auth_mode=mode))
        else:
            specs.append(AgentSpec(role=r.value))
    specs += extra_specs or []
    reg = AgentRegistry(specs, all_mock_adapters(), ENV if env is None else env, TEST_PRICES,
                        real_factories(cli_runner=runner, executor_loop=loop, handoff=handoff), redactor=repo.redactor,
                        policy=policy or POLICY)
    svc = ArtifactService(repo, ArtifactStore(tmp_path / "cas", tmp_path / "eval"))
    worker = Worker(repo, reg, FreshDirRunner(tmp_path / "ws"), svc, "w-real", host_environ=host_environ if host_environ is not None else {})
    return World(worker, reg, svc, loop, runner)


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


def result_writer(spec: LoopSpec) -> None:
    (spec.cwd / "out").mkdir(exist_ok=True)
    (spec.cwd / "out" / "result.md").write_bytes(b"# executor result\n")


def pool_used(repo: Any, role: str | None = None) -> int:
    """Spend booked against the monthly agent_sdk_credit pool (role=None) or a role allocation."""
    from airos.persistence import budget_sql

    now = repo.clock.now_ms()
    bid = budget_sql.pool_budget_id(now) if role is None else budget_sql.alloc_budget_id(role, now)
    try:
        return int(repo.budget_status(bid).used_usd_micro)
    except Exception:  # noqa: BLE001 - budget rows are created lazily; none yet means nothing was booked
        return 0
