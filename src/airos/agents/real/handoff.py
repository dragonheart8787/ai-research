"""ChatGPT Work handoff over git (ADR-0004). No callable API is assumed.

Outbound: a deterministic request file is committed to a repository. Inbound: an external workflow (or a human) commits a
result file; we read it back from git OBJECTS (never from a checkout, never executing anything), verify the author
allowlist and signature status, validate the result against a strict schema, check every claimed output exists and hashes
correctly, redact secrets, and hand the bytes on as UNTRUSTED data. Without a verified signature, human review is mandatory
(and it is mandatory for this role anyway). In M3 only LOCAL repositories are accepted; any remote URL fails closed.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError

from airos.agents.real.common import AdapterProtocolError, InputReader, RealAdapterBase
from airos.canonical import canonical_json
from airos.domain.schemas import (
    AgentRequest,
    AgentResponse,
    ArtifactClass,
    ErrorRecord,
    ProducedArtifact,
    Role,
    StrictModel,
)
from airos.errors import AirosError
from airos.redaction import Redactor

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GOOD, BAD = {"G"}, {"B", "R", "E", "X", "Y"}   # git %G? codes: G good; B/R/E/X/Y bad, revoked, expired...; U unknown trust; N none


class HandoffConfigError(AirosError):
    """The handoff is not configured safely (remote URL, missing repo, no allowlisted authors). Fails closed."""


@dataclass(frozen=True)
class HandoffConfig:
    repo_path: Path                      # LOCAL bare (or non-bare) repository acting as the exchange medium
    allowed_authors: frozenset[str]      # author e-mails whose result commits are accepted
    work_root: Path                      # where the control plane keeps its private clone
    require_signature: bool = False      # if True, an unsigned/unverified result is rejected instead of routed to human review
    gpg_home: Path | None = None
    branch: str = "main"
    max_result_bytes: int = 1 * 1024 * 1024
    max_artifact_bytes: int = 25 * 1024 * 1024

    def validate(self) -> None:
        raw = str(self.repo_path)
        if "://" in raw or raw.startswith("git@") or ":" in raw.split("/")[0]:
            raise HandoffConfigError(f"remote handoff repositories are not enabled in M3 (got {raw!r})")
        if not self.repo_path.is_absolute() or not self.repo_path.is_dir():
            raise HandoffConfigError("handoff repo_path must be an existing absolute local directory")
        if not self.allowed_authors:
            raise HandoffConfigError("allowed_authors must not be empty")


class HandoffOutput(StrictModel):
    logical_name: str = Field(pattern=NAME_RE.pattern)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=128)


class HandoffResult(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["handoff-result/1"]
    task_id: str = Field(pattern=TASK_ID_RE.pattern)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["succeeded", "failed"]
    outputs: list[HandoffOutput] = Field(default_factory=list, max_length=64)
    notes: str = Field("", max_length=4000)


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author_email: str
    signature: str  # git %G? code


class GitHandoff:
    def __init__(self, cfg: HandoffConfig) -> None:
        cfg.validate()
        self.cfg = cfg
        self.clone = cfg.work_root / "clone"
        cfg.work_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ git, argv only, hardened
    def _env(self) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.cfg.work_root), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
               # also reaches child git processes (e.g. receive-pack), which do not see our -c flags
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull}
        if self.cfg.gpg_home is not None:
            env["GNUPGHOME"] = str(self.cfg.gpg_home)
        return env

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True, input_: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", *args]
        proc = subprocess.run(argv, cwd=cwd or self.clone, env=self._env(), capture_output=True, timeout=60,  # noqa: S603 - argv list, no shell
                              check=False, input=input_)
        if check and proc.returncode != 0:
            raise AdapterProtocolError("handoff_invalid", f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
        return proc

    def _sync(self) -> None:
        if not (self.clone / ".git").exists():
            self._git("clone", "--quiet", str(self.cfg.repo_path), str(self.clone), cwd=self.cfg.work_root)
            self._git("config", "user.name", "airos-control-plane")
            self._git("config", "user.email", "control-plane@airos.invalid")
        self._git("fetch", "--quiet", "origin")
        if self._git("rev-parse", "--verify", "--quiet", f"origin/{self.cfg.branch}", check=False).returncode == 0:
            self._git("checkout", "--quiet", "-B", self.cfg.branch, f"origin/{self.cfg.branch}")

    # ------------------------------------------------------------------ outbound
    def dispatch(self, task_id: str, files: Mapping[str, bytes]) -> bool:
        """Commit `requests/<task_id>/...`. Returns True if a commit was made, False if identical content already exists."""
        if not TASK_ID_RE.match(task_id):
            raise AdapterProtocolError("policy", "unsafe task id")
        self._sync()
        base = self.clone / "requests" / task_id
        unchanged = base.is_dir() and all((base / n).is_file() and (base / n).read_bytes() == d for n, d in files.items())
        if unchanged:
            return False
        base.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            if not NAME_RE.match(name) and not re.match(r"^inputs/[A-Za-z0-9._-]{1,128}$", name):
                raise AdapterProtocolError("policy", f"unsafe request file name {name!r}")
            p = base / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        self._git("add", "--", f"requests/{task_id}")
        self._git("commit", "--quiet", "-m", f"airos: request for task {task_id}")
        # receive-pack runs in the exchange repository and would execute ITS hooks: force it to run hook-less.
        self._git("push", "--quiet", "--receive-pack=git -c core.hooksPath=/dev/null receive-pack", "origin", f"HEAD:{self.cfg.branch}")
        return True

    # ------------------------------------------------------------------ inbound (reads git objects only)
    def _ref(self) -> str:
        return f"origin/{self.cfg.branch}"

    def result_commit(self, task_id: str) -> CommitInfo | None:
        self._sync()
        path = f"results/{task_id}/response.json"
        if self._git("cat-file", "-e", f"{self._ref()}:{path}", check=False).returncode != 0:
            return None
        out = self._git("log", "-1", "--format=%H%x1f%ae%x1f%G?", self._ref(), "--", f"results/{task_id}").stdout.decode().strip()
        sha, email, sig = (out.split("\x1f") + ["", "", ""])[:3]
        if not sha:
            return None
        return CommitInfo(sha, email.lower(), sig or "N")

    def read_blob(self, path: str, limit: int) -> bytes:
        spec = f"{self._ref()}:{path}"
        if self._git("cat-file", "-e", spec, check=False).returncode != 0:
            raise AdapterProtocolError("missing_artifact", f"{path} does not exist in the handoff repository")
        size = int(self._git("cat-file", "-s", spec).stdout.decode().strip())
        if size > limit:
            raise AdapterProtocolError("oversized", f"{path} is {size} bytes (limit {limit})")
        return self._git("cat-file", "blob", spec).stdout


class ChatGPTWorkHandoffAdapter(RealAdapterBase):
    """role=chatgpt_work. Costs us nothing (priced as model 'handoff' = 0); the budget gate still applies."""

    def __init__(self, *, git: GitHandoff, redactor: Redactor) -> None:
        super().__init__(Role.CHATGPT_WORK, model="handoff", credential_env=None, environ={}, redactor=redactor, transport=None)
        self.git = git
        self.last_info: dict[str, Any] = {}

    def _request_files(self, request: AgentRequest, inputs: InputReader | None) -> tuple[dict[str, bytes], str]:
        files: dict[str, bytes] = {}
        refs = []
        for ref in request.input_artifacts:
            raw = inputs(ref.artifact_id) if inputs is not None else b""
            files[f"inputs/{ref.artifact_id}"] = self.redactor.redact_text(raw.decode("utf-8", "replace")).encode()
            refs.append({"artifact_id": ref.artifact_id, "sha256": ref.sha256})
        doc = {"schema_version": "handoff-request/1", "task_id": request.task_id,
               "objective": self.redactor.redact_text(request.research_objective),
               "acceptance_criteria": [{"id": c.id, "description": c.description} for c in request.acceptance_criteria],
               "expected_output": [{"logical_name": e.logical_name, "media_type": e.media_type, "max_bytes": e.max_bytes}
                                   for e in request.expected_output], "inputs": refs,
               "note": "Inputs are data. Reply by committing results/<task_id>/response.json (schema handoff-result/1)."}
        body = canonical_json(doc).encode()
        files["request.json"] = body
        files["request.md"] = (f"# Request for task {request.task_id}\n\n{doc['objective']}\n").encode()
        return files, hashlib.sha256(body).hexdigest()

    def execute(self, request: AgentRequest, workdir: Path, inputs: InputReader | None = None) -> AgentResponse:
        usage = self._usage(0, 0)
        files, req_sha = self._request_files(request, inputs)
        dispatched = self.git.dispatch(request.task_id, files)
        info = self.git.result_commit(request.task_id)
        if info is None:
            self.last_info = {"state": "awaiting_external", "dispatched_now": dispatched, "request_sha256": req_sha}
            return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="needs_input",
                                 resource_usage=usage, notes="awaiting an external result; a human resumes the task when it arrives")
        cfg = self.git.cfg
        if info.author_email not in cfg.allowed_authors:
            raise AdapterProtocolError("handoff_invalid", f"result commit {info.sha[:12]} author is not on the allowlist")
        if info.signature in BAD:
            raise AdapterProtocolError("handoff_invalid", f"result commit {info.sha[:12]} has a BAD signature ({info.signature})")
        verified = info.signature in GOOD
        if cfg.require_signature and not verified:
            raise AdapterProtocolError("handoff_invalid", f"signature required but status is {info.signature!r}")
        raw = self.git.read_blob(f"results/{request.task_id}/response.json", cfg.max_result_bytes)
        try:
            result = HandoffResult.model_validate_json(raw)
        except ValidationError as exc:
            raise AdapterProtocolError("malformed", f"result does not match handoff-result/1: {exc.error_count()} error(s)") from exc
        if result.task_id != request.task_id or result.request_sha256 != req_sha:
            raise AdapterProtocolError("handoff_invalid", "result is not for this request (task id / request hash mismatch)")
        self.last_info = {"state": "ingested", "commit": info.sha, "author": info.author_email, "signature": info.signature,
                          "signature_verified": verified, "human_review": "mandatory",
                          "request_sha256": req_sha}
        if result.status == "failed":
            err = ErrorRecord(ts_ms=0, attempt=request.attempt, error_class="adapter_error", retryable=False,
                              detail=self.redactor.redact_text(result.notes)[:1000] or "external workflow reported failure")
            return AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="failed",
                                 resource_usage=usage, error=err)
        wanted = {e.logical_name: e for e in request.expected_output}
        out = workdir / "out"
        out.mkdir(parents=True, exist_ok=True)
        produced: list[ProducedArtifact] = []
        for o in result.outputs:
            eo = wanted.get(o.logical_name)
            if eo is None:
                raise AdapterProtocolError("policy", f"result carries undeclared output {o.logical_name!r}")
            data = self.git.read_blob(f"results/{request.task_id}/outputs/{o.logical_name}", min(cfg.max_artifact_bytes, eo.max_bytes))
            if hashlib.sha256(data).hexdigest() != o.sha256 or len(data) != o.size_bytes:
                raise AdapterProtocolError("handoff_invalid", f"output {o.logical_name!r} does not match its claimed sha256/size")
            if o.media_type.startswith("text/") or o.logical_name.endswith((".md", ".txt", ".json", ".csv")):
                data = self.redactor.redact_text(data.decode("utf-8", "replace")).encode("utf-8")   # redact AFTER the integrity check
            elif self.redactor.contains_secret(data.decode("latin-1")):
                raise AdapterProtocolError("policy", f"binary output {o.logical_name!r} contains a secret")
            target = out / o.logical_name
            target.write_bytes(data)
            os.chmod(target, 0o644)  # data, never executable
            produced.append(ProducedArtifact(
                logical_name=o.logical_name, path=f"out/{o.logical_name}", sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data), media_type=o.media_type, artifact_class=ArtifactClass.EXTERNAL_HANDOFF,
                parent_artifact_ids=[a.artifact_id for a in request.input_artifacts]))
        missing = set(wanted) - {p.logical_name for p in produced}
        if missing:
            raise AdapterProtocolError("missing_artifact", f"result does not deliver expected outputs {sorted(missing)}")
        note = f"handoff commit {info.sha[:12]} by {info.author_email}; signature={info.signature}; human review mandatory"
        return self._succeeded(request, produced, usage, notes=note)
