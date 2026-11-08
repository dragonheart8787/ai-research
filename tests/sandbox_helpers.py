"""Shared fixtures for the M4a sandbox tests. SPEC-ONLY: nothing here starts a container."""
from __future__ import annotations

from pathlib import Path

from airos.auth import build_adapter_env
from airos.sandbox.spec import RunSpec, SandboxPolicy, build_run_spec

DIGEST = "sha256:" + "0" * 64            # a PLACEHOLDER digest: no such image exists
GOLDEN = Path(__file__).parent / "golden" / "sandbox"
RUN_ROOT = Path("/srv/airos/data/workspaces")
PROTECTED = (Path("/srv/airos/data/eval-holdout"), Path("/srv/airos/data/cas"))
CRED_SOURCE = {"planner": "AIROS_CLAUDE_TOKEN_PLANNER", "executor": "AIROS_CLAUDE_TOKEN_EXECUTOR"}
COMMANDS = {"planner": ("claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", "", "--model", "claude-opus-5-5"),
            "executor": ("airos-executor-loop",),
            "director": ("codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--model", "director-test-model", "-")}
TOKEN = "sk-ant-" + "oat01-golden-" + "T" * 20


def policy(run_root: Path = RUN_ROOT, **over: object) -> SandboxPolicy:
    base: dict[str, object] = {"run_root": run_root, "images": {r: f"airos-agent-{r}@{DIGEST}" for r in ("planner", "executor", "director")},
                               "protected_roots": PROTECTED}
    base.update(over)
    return SandboxPolicy(**base)  # type: ignore[arg-type]


def spec_for(role: str, run_id: str = "run0001", pol: SandboxPolicy | None = None, with_token: bool = True) -> RunSpec:
    env = build_adapter_env(role, credential_value=TOKEN if with_token and role in CRED_SOURCE else None)
    return build_run_spec(pol or policy(), role, run_id, COMMANDS[role], env=env, credential_source_env=CRED_SOURCE.get(role))
