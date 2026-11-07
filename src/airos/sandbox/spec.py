"""Container run-spec generator (PROPOSAL 11.1, ADR-0006). SPEC-ONLY: it produces a typed spec and a `docker run` argument vector; it never
executes anything. The golden files built from it are labelled "spec-only, isolation not verified".

Rules enforced by `validate_run_spec` (and therefore by `to_docker_argv`, which refuses an invalid spec):
  * argv arrays only (no shell), never privileged, no extra capabilities, no-new-privileges, non-root uid, read-only rootfs,
  * mounts are exactly work (rw), inputs (ro), out (rw) under data/workspaces/<run_id>/, plus (Director only) its own login volume;
    never the Docker socket, "/", home, the repo, config, the database, the CAS root or final-evaluation data,
  * the image is pinned by digest (a local dev tag is accepted only when the policy says so),
  * credentials: names only. A secret value is NEVER part of the argv: it is handed to the docker CLIENT process environment and forwarded
    by name (`-e NAME`). Each role has its own network, its own volumes and its own host credential variable.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from airos.auth import (
    CLAUDE_ROLES,
    CLAUDE_TOKEN_VAR,
    CODEX_HOME_IN_CONTAINER,
    build_adapter_env,
    is_forbidden_env_name,
)
from airos.errors import AirosError
from airos.redaction import Redactor

SPEC_ONLY_LABEL = "spec-only, isolation not verified"
CONTAINER_ROLES = ("planner", "executor", "director")          # roles that run in a container; chatgpt_work is manual, ai_scientist is a stub
UID = GID = 10001
DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/\-]*@sha256:[0-9a-f]{64}$")
LOCAL_TAG_RE = re.compile(r"^airos-local/[a-z0-9][a-z0-9._\-]*:[A-Za-z0-9._\-]+$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,62}$")
DENY_SUBSTRINGS = ("docker.sock", "eval-holdout", "final_evaluation", "airos.db", ".env", "/config", ".git", "/etc", "/root", "/home", "/proc", "/sys")
MAX_PIDS = 256
WORK, INPUTS, OUT = "/work", "/inputs", "/outputs"


class SpecError(AirosError):
    """A run spec could not be built, or is invalid. Always fail closed."""


@dataclass(frozen=True)
class Mount:
    kind: Literal["bind", "volume", "tmpfs"]
    source: str            # host path (bind), volume name (volume), "" (tmpfs)
    target: str
    read_only: bool
    options: str = ""      # tmpfs only (size, noexec, ...)

    def render(self) -> str:
        if self.kind == "tmpfs":
            raise ValueError("tmpfs mounts are rendered with --tmpfs")
        ro = ",readonly" if self.read_only else ""
        return f"type={self.kind},source={self.source},target={self.target}{ro}"


@dataclass(frozen=True)
class SandboxPolicy:
    """Human-set limits for the sandbox. `run_root` is the ONLY host directory runs may mount from."""

    run_root: Path
    images: Mapping[str, str]                              # role -> image reference (digest-pinned)
    cpus: float = 2.0
    memory_gib: int = 4
    pids_limit: int = MAX_PIDS
    tmp_size: str = "256m"
    require_image_digest: bool = True                       # False only for local dev/tests; the spec then says so
    protected_roots: tuple[Path, ...] = ()                  # e.g. the final-evaluation store, CAS root, DB dir: never mountable, never an ancestor of a mount
    codex_login_volume: str = "airos-codex-director"
    network_prefix: str = "airos-net-"
    proxy_host_prefix: str = "airos-egress-"
    docker_host: str | None = None                          # e.g. unix:///run/user/1000/docker.sock (rootless); never inherited from the shell


@dataclass(frozen=True)
class RunSpec:
    role: str
    run_id: str
    name: str
    image: str
    command: tuple[str, ...]
    user: str
    read_only_rootfs: bool
    privileged: bool
    cap_drop: tuple[str, ...]
    cap_add: tuple[str, ...]
    security_opts: tuple[str, ...]
    network: str
    ipc: str
    pids_limit: int
    cpus: float
    memory: str
    memory_swap: str
    nofile: int
    mounts: tuple[Mount, ...]
    env_literals: tuple[tuple[str, str], ...]
    env_from_client: tuple[str, ...]                        # names only; values travel in the docker CLIENT's environment
    credential_source_env: str | None                       # the HOST variable holding this role's token (a name, never a value)
    labels: tuple[tuple[str, str], ...]
    workdir: str = WORK
    image_pinned: bool = True
    pull: str = "never"
    interactive: bool = True                                # prompts travel over stdin (never argv)
    notes: tuple[str, ...] = field(default=(SPEC_ONLY_LABEL,))


def _norm_host_path(p: str) -> str:
    return os.path.normpath(os.path.realpath(p))


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _mount_problems(spec: RunSpec, policy: SandboxPolicy) -> list[str]:
    problems: list[str] = []
    run_dir = _norm_host_path(str(policy.run_root / spec.run_id))
    protected = [_norm_host_path(str(p)) for p in policy.protected_roots]
    seen_targets: set[str] = set()
    for m in spec.mounts:
        if m.target in seen_targets:
            problems.append(f"duplicate mount target {m.target}")
        seen_targets.add(m.target)
        if not m.target.startswith("/") or ".." in m.target.split("/"):
            problems.append(f"mount target {m.target!r} is not a clean absolute path")
        if m.kind == "tmpfs":
            continue
        if m.kind == "volume":
            if m.source != policy.codex_login_volume or spec.role != "director":
                problems.append(f"named volume {m.source!r} is only allowed as the Director's own login volume")
            elif m.target != CODEX_HOME_IN_CONTAINER or m.read_only:
                problems.append("the Director login volume must be mounted read-write at /codex-home only")
            continue
        src = m.source
        if not src.startswith("/") or ".." in src.split("/"):
            problems.append(f"bind source {src!r} is not a clean absolute path")
            continue
        real = _norm_host_path(src)
        low = real.lower()
        if any(d in low for d in DENY_SUBSTRINGS) and not _within(real, run_dir):
            problems.append(f"bind source {src!r} matches the deny list")
        if real == "/" or not _within(real, run_dir) or real == run_dir:
            problems.append(f"bind source {src!r} is not a per-run directory under {run_dir}")
        for prot in protected:
            if _within(real, prot) or _within(prot, real):
                problems.append(f"bind source {src!r} overlaps protected data {prot!r}")
        if m.target in (WORK, OUT) and m.read_only:
            problems.append(f"{m.target} must be writable")
        if m.target == INPUTS and not m.read_only:
            problems.append("/inputs must be read-only")
        if m.target not in (WORK, INPUTS, OUT):
            problems.append(f"bind target {m.target!r} is not one of /work, /inputs, /outputs")
    binds = {m.target for m in spec.mounts if m.kind == "bind"}
    if binds != {WORK, INPUTS, OUT}:
        problems.append(f"bind mounts must be exactly /work, /inputs, /outputs (got {sorted(binds)})")
    return problems


def validate_run_spec(spec: RunSpec, policy: SandboxPolicy) -> list[str]:
    """All problems found (empty list = valid). A spec with ANY problem must not be used."""
    p: list[str] = []
    if spec.role not in CONTAINER_ROLES:
        p.append(f"role {spec.role!r} does not run in a container")
    if spec.privileged:
        p.append("privileged containers are forbidden")
    if spec.cap_add:
        p.append(f"capabilities may not be added: {list(spec.cap_add)}")
    if spec.cap_drop != ("ALL",):
        p.append("cap_drop must be exactly ALL")
    if "no-new-privileges:true" not in spec.security_opts:
        p.append("no-new-privileges:true is required")
    if any(o.startswith(("seccomp=unconfined", "apparmor=unconfined", "label=disable", "systempaths=unconfined")) for o in spec.security_opts):
        p.append("security profiles may not be disabled")
    if spec.user != f"{UID}:{GID}":
        p.append(f"user must be {UID}:{GID} (non-root)")
    if not spec.read_only_rootfs:
        p.append("root filesystem must be read-only")
    if spec.network in ("host", "bridge", "default", "none", "") or spec.network.startswith("container:"):
        p.append(f"network {spec.network!r} is not a dedicated per-role network")
    elif spec.network != f"{policy.network_prefix}{spec.role}":
        p.append(f"network must be {policy.network_prefix}{spec.role}")
    if spec.ipc != "none":
        p.append("ipc must be none")
    if not (0 < spec.pids_limit <= MAX_PIDS):
        p.append(f"pids_limit must be 1..{MAX_PIDS}")
    if spec.cpus <= 0 or spec.cpus > 16:
        p.append("cpus out of range")
    if spec.memory_swap != spec.memory:
        p.append("memory_swap must equal memory (no swap)")
    if spec.pull != "never":
        p.append("images are never pulled by a run (pull must be 'never')")
    if not NAME_RE.fullmatch(spec.name):
        p.append(f"container name {spec.name!r} is not valid")
    if policy.require_image_digest:
        if not DIGEST_RE.fullmatch(spec.image):
            p.append("image must be pinned by digest (name@sha256:<64 hex>)")
    elif not (DIGEST_RE.fullmatch(spec.image) or LOCAL_TAG_RE.fullmatch(spec.image)):
        p.append("image must be digest-pinned or an airos-local/ dev tag")
    if not spec.command or any(not isinstance(c, str) for c in spec.command) or spec.command[0] in ("sh", "bash", "/bin/sh", "/bin/bash", "dash"):
        p.append("command must be a non-empty argv without a shell")
    if spec.workdir != WORK:
        p.append("workdir must be /work")
    p += _mount_problems(spec, policy)
    red = Redactor()
    for k, v in spec.env_literals:
        if is_forbidden_env_name(k):
            p.append(f"environment variable {k} is forbidden")
        if red.contains_secret(f"{k}={v}") or k == CLAUDE_TOKEN_VAR:
            p.append(f"environment literal {k} looks like a secret; credentials travel by name only")
    allowed_from_client = (CLAUDE_TOKEN_VAR,) if spec.role in CLAUDE_ROLES else ()
    if spec.role == "director" and spec.credential_source_env is not None:
        p.append("the Director has no token: its login state lives in its own volume")
    for n in spec.env_from_client:
        if n not in allowed_from_client:
            p.append(f"environment variable {n} may not be forwarded into the {spec.role} container")
    if spec.role in CLAUDE_ROLES and spec.env_from_client and not spec.credential_source_env:
        p.append("a forwarded credential needs a named host source variable")
    if spec.credential_source_env and is_forbidden_env_name(spec.credential_source_env):
        p.append(f"credential source {spec.credential_source_env} is a forbidden (billing) variable name")
    if spec.credential_source_env and spec.credential_source_env == CLAUDE_TOKEN_VAR:
        p.append("the host credential variable must differ from the in-container name")
    labels = dict(spec.labels)
    if labels.get("airos.run_id") != spec.run_id or labels.get("airos.role") != spec.role:
        p.append("labels airos.run_id / airos.role are required and must match")
    return p


def build_run_spec(policy: SandboxPolicy, role: str, run_id: str, command: Sequence[str], *, env: Mapping[str, str] | None = None,
                   credential_source_env: str | None = None, cpus: float | None = None, memory_gib: int | None = None,
                   instance: str = "") -> RunSpec:
    """Build the spec for one run. `env` is the adapter's complete environment (auth.build_adapter_env); the token, if present, is NOT
    copied into the spec: only its in-container NAME is forwarded."""
    if role == "chatgpt_work":
        raise SpecError("chatgpt_work is a manual handoff: it has no container")
    if role == "ai_scientist":
        raise SpecError("ai_scientist is a fail-closed stub until M5 (ADR-0005): no container spec can be built for it")
    if role not in CONTAINER_ROLES:
        raise SpecError(f"unknown role {role!r}")
    if role not in policy.images:
        raise SpecError(f"no image configured for role {role!r}")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", run_id) or not re.fullmatch(r"[a-z0-9]{0,8}", instance):
        raise SpecError(f"run_id {run_id!r} / instance {instance!r} is not safe to use in names and paths")
    full_env = dict(env) if env is not None else build_adapter_env(role)
    token_present = CLAUDE_TOKEN_VAR in full_env
    literals = {k: v for k, v in full_env.items() if k != CLAUDE_TOKEN_VAR}
    run_dir = policy.run_root / run_id
    mem = memory_gib if memory_gib is not None else policy.memory_gib
    mounts: list[Mount] = [
        Mount("bind", str(run_dir / "work"), WORK, False),
        Mount("bind", str(run_dir / "inputs"), INPUTS, True),
        Mount("bind", str(run_dir / "out"), OUT, False),
        Mount("tmpfs", "", "/tmp", False, policy.tmp_size),  # noqa: S108 - a path INSIDE the container
    ]
    if role == "director":
        mounts.append(Mount("volume", policy.codex_login_volume, CODEX_HOME_IN_CONTAINER, False))
    # all traffic is forced through this role's own allow-list proxy on its own internal network (egress policy: config/egress.toml)
    proxy = f"http://{policy.proxy_host_prefix}{role}:3128"
    literals.update({"HTTPS_PROXY": proxy, "https_proxy": proxy, "NO_PROXY": ""})
    spec = RunSpec(
        role=role, run_id=run_id, name=(f"airos-{role}-{run_id}" + (f"-{instance}" if instance else "")).lower(), image=policy.images[role], command=tuple(command),
        user=f"{UID}:{GID}", read_only_rootfs=True, privileged=False, cap_drop=("ALL",), cap_add=(),
        security_opts=("no-new-privileges:true",), network=f"{policy.network_prefix}{role}", ipc="none", pids_limit=policy.pids_limit,
        cpus=cpus if cpus is not None else policy.cpus, memory=f"{mem}g", memory_swap=f"{mem}g", nofile=1024, mounts=tuple(mounts),
        env_literals=tuple(sorted(literals.items())), env_from_client=(CLAUDE_TOKEN_VAR,) if token_present else (),
        credential_source_env=credential_source_env if token_present else None,
        labels=(("airos.role", role), ("airos.run_id", run_id), ("airos.spec", "spec-only")),
        image_pinned=bool(DIGEST_RE.fullmatch(policy.images[role])),
    )
    problems = validate_run_spec(spec, policy)
    if problems:
        raise SpecError("invalid run spec: " + "; ".join(problems))
    return spec


def to_docker_argv(spec: RunSpec, policy: SandboxPolicy) -> list[str]:
    """The `docker run` argument vector (an array, never a string). Refuses an invalid spec. Contains no secret value."""
    problems = validate_run_spec(spec, policy)
    if problems:
        raise SpecError("refusing to render an invalid run spec: " + "; ".join(problems))
    a: list[str] = ["docker", "run", "--rm", "--init", *(["--interactive"] if spec.interactive else []), "--pull", spec.pull, "--name", spec.name, "--user", spec.user, "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--network", spec.network, "--ipc", spec.ipc,
                    "--cpus", f"{spec.cpus:g}", "--memory", spec.memory, "--memory-swap", spec.memory_swap, "--pids-limit",
                    str(spec.pids_limit), "--ulimit", f"nofile={spec.nofile}:{spec.nofile}"]
    for k, v in spec.labels:
        a += ["--label", f"{k}={v}"]
    for m in spec.mounts:
        if m.kind == "tmpfs":
            a += ["--tmpfs", f"{m.target}:rw,noexec,nosuid,size={m.options or '64m'}"]
        else:
            a += ["--mount", m.render()]
    for k, v in spec.env_literals:
        a += ["--env", f"{k}={v}"]
    for n in spec.env_from_client:
        a += ["--env", n]                      # name only: docker reads the value from ITS OWN environment, never from argv
    a += ["--workdir", spec.workdir, spec.image, *spec.command]
    return a


def render_golden(spec: RunSpec, policy: SandboxPolicy) -> str:
    """Stable text form used by the golden-file tests: one flag (with its value) per line, then the image, then the in-container command."""
    argv = to_docker_argv(spec, policy)
    split = len(argv) - len(spec.command) - 1            # index of the image
    head, image, command = argv[:split], argv[split], argv[split + 1:]
    lines = [f"# {SPEC_ONLY_LABEL}: generated by airos.sandbox.spec; no Docker daemon was available, so none of these flags was exercised.",
             f"# role={spec.role} image_pinned={spec.image_pinned} credential_source_env={spec.credential_source_env}", "docker run"]
    i = 2
    bare = {"--rm", "--init", "--read-only", "--interactive"}
    while i < len(head):
        if head[i] in bare:
            lines.append(head[i])
            i += 1
        else:
            lines.append(f"{head[i]} {head[i + 1]}")
            i += 2
    lines += [f"IMAGE {image}", "COMMAND " + " ".join(command if command else ["<none>"])]
    return "\n".join(lines) + "\n"


def isolation_overlap(a: RunSpec, b: RunSpec) -> list[str]:
    """TSEC-05: two roles must share no network, writable volume, host credential variable, container name or proxy."""
    overlaps: list[str] = []
    if a.network == b.network:
        overlaps.append(f"network {a.network}")
    va = {m.source for m in a.mounts if m.kind == "volume"}
    vb = {m.source for m in b.mounts if m.kind == "volume"}
    if va & vb:
        overlaps.append(f"volume {sorted(va & vb)}")
    if a.credential_source_env and a.credential_source_env == b.credential_source_env:
        overlaps.append(f"credential variable {a.credential_source_env}")
    if a.name == b.name:
        overlaps.append("container name")
    ha = {v for k, v in a.env_literals if k == "HTTPS_PROXY"}
    hb = {v for k, v in b.env_literals if k == "HTTPS_PROXY"}
    if ha & hb:
        overlaps.append("egress proxy")
    return overlaps

