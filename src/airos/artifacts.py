"""Content-addressed artifact store, safe path handling, and the artifact service (ingest / verify / read).

* Bytes live in a CAS keyed by sha256. Final-evaluation data lives under a DIFFERENT root (`eval_root`) that is never
  inside the general CAS or any run workspace.
* Ingest accepts only a plain relative path to a regular, single-link file strictly inside the run directory.
  Path traversal, absolute paths, symlinks at any component, hard links, non-regular files and anything resolving under
  a forbidden root (eval root, CAS root) are rejected.
"""
from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from airos.canonical import sha256_hex
from airos.domain.schemas import (
    Artifact,
    ArtifactClass,
    ProducedArtifact,
    TaskRecord,
    TrustLevel,
    provenance_hash,
)
from airos.domain.states import Actor
from airos.errors import IntegrityError, UnsafePath
from airos.persistence.sqlite_repo import SYSTEM, SqliteRepository

MAX_INGEST_BYTES = 256 * 1024 * 1024


def safe_resolve_in(base: Path, rel: str, forbidden: tuple[Path, ...] = ()) -> Path:
    """Resolve `rel` inside `base` or raise UnsafePath. Never follows symlinks."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/") or Path(rel).is_absolute():
        raise UnsafePath(f"unsafe artifact path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"path traversal or empty component in {rel!r}")
    base_real = base.resolve(strict=True)
    cur = base_real
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError as exc:
            raise UnsafePath(f"{rel!r} does not exist") from exc
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePath(f"symlink component in {rel!r}")
        last = i == len(parts) - 1
        if last:
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(f"{rel!r} is not a regular file")
            if st.st_nlink != 1:
                raise UnsafePath(f"{rel!r} has multiple hard links")
        elif not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"non-directory component in {rel!r}")
    resolved = cur.resolve(strict=True)
    if base_real not in resolved.parents:
        raise UnsafePath(f"{rel!r} escapes the run directory")
    for bad in forbidden:
        bad_real = bad.resolve()
        if resolved == bad_real or bad_real in resolved.parents:
            raise UnsafePath(f"{rel!r} is inside a forbidden root")
    return resolved


class ArtifactStore:
    def __init__(self, root: Path, eval_root: Path) -> None:
        root, eval_root = root.resolve(), eval_root.resolve()
        if root == eval_root or root in eval_root.parents or eval_root in root.parents:
            raise ValueError("eval_root must be disjoint from the general artifact root")
        root.mkdir(parents=True, exist_ok=True)
        eval_root.mkdir(parents=True, exist_ok=True)
        self.root, self.eval_root = root, eval_root
        self.reads: list[str] = []  # sha256 of every blob opened for reading (tests assert agents never trigger one)

    def _base(self, final_eval: bool) -> Path:
        return self.eval_root if final_eval else self.root

    def path_for(self, sha256: str, final_eval: bool = False) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("bad sha256")
        return self._base(final_eval) / sha256[:2] / sha256[2:4] / sha256

    def put(self, data: bytes, *, final_eval: bool = False) -> str:
        sha = sha256_hex(data)
        dest = self.path_for(sha, final_eval)
        if dest.exists():
            if sha256_hex(dest.read_bytes()) != sha:
                raise IntegrityError(f"stored blob {sha} is corrupt")
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o444)
            os.replace(tmp, dest)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return sha

    def read(self, sha256: str, *, final_eval: bool = False) -> bytes:
        self.reads.append(sha256)
        return self.path_for(sha256, final_eval).read_bytes()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


class ArtifactService:
    def __init__(self, repo: SqliteRepository, store: ArtifactStore) -> None:
        self.repo, self.store = repo, store

    def _forbidden(self) -> tuple[Path, ...]:
        return (self.store.root, self.store.eval_root)

    def ingest(self, *, run_dir: Path, produced: ProducedArtifact, run_id: str, task: TaskRecord,
               artifact_id: str, is_mock: bool, max_bytes: int = MAX_INGEST_BYTES, actor: Actor = SYSTEM) -> Artifact:
        """Copy one agent-produced file into the CAS and register it. Recomputes the hash from the bytes."""
        path = safe_resolve_in(run_dir, produced.path, self._forbidden())
        size = path.stat().st_size
        if size > max_bytes:
            raise UnsafePath(f"{produced.path!r} is {size} bytes, over the {max_bytes} byte limit")
        data = path.read_bytes()
        sha = sha256_hex(data)
        if sha != produced.sha256 or len(data) != produced.size_bytes:
            raise IntegrityError(
                f"hash_mismatch for {produced.logical_name}: response claims {produced.sha256}, bytes hash to {sha}")
        final = produced.artifact_class is ArtifactClass.FINAL_EVALUATION
        red = self.repo.redactor
        if red.contains_secret(data.decode("latin-1")):
            if not (produced.media_type.startswith("text/") or produced.media_type == "application/json"):
                raise UnsafePath(f"{produced.logical_name}: binary artifact contains a secret; refusing to store it")
            data = red.redact_text(data.decode("utf-8", "replace")).encode("utf-8")  # integrity was checked on the ORIGINAL bytes
            sha = sha256_hex(data)
        self.store.put(data, final_eval=final)
        art = Artifact(
            artifact_id=artifact_id, sha256=sha, size_bytes=len(data), media_type=produced.media_type,
            artifact_class=produced.artifact_class, logical_name=produced.logical_name,
            storage_uri=f"cas://{sha}", producer_run_id=run_id, trust_level=TrustLevel.TRUSTED, is_mock=is_mock,
            parent_artifact_ids=list(produced.parent_artifact_ids), created_at_ms=self.repo.clock.now_ms(),
        )
        return self.repo.add_artifact(art, actor)  # the repository forces trust level / class-role pairing

    def import_bytes(self, *, data: bytes, artifact_id: str, logical_name: str, media_type: str,
                     artifact_class: ArtifactClass, human: Actor, is_mock: bool = False,
                     parent_artifact_ids: list[str] | None = None) -> Artifact:
        """Manual handoff / human import: the human principal is the producer of record."""
        final = artifact_class is ArtifactClass.FINAL_EVALUATION
        sha = self.store.put(data, final_eval=final)
        art = Artifact(
            artifact_id=artifact_id, sha256=sha, size_bytes=len(data), media_type=media_type,
            artifact_class=artifact_class, logical_name=logical_name, storage_uri=f"cas://{sha}",
            producer_principal_id=human.id, trust_level=TrustLevel.TRUSTED, is_mock=is_mock,
            parent_artifact_ids=parent_artifact_ids or [], created_at_ms=self.repo.clock.now_ms(),
        )
        return self.repo.add_artifact(art, human)

    def verify(self, artifact_id: str) -> VerifyResult:
        """Recompute the content hash from the STORED bytes and the provenance hash from the registry row."""
        problems: list[str] = []
        art = self.repo.get_artifact(artifact_id)
        final = art.artifact_class is ArtifactClass.FINAL_EVALUATION
        try:
            data = self.store.path_for(art.sha256, final).read_bytes()
        except FileNotFoundError:
            return VerifyResult(False, [f"stored bytes for {art.sha256} are missing"])
        if sha256_hex(data) != art.sha256:
            problems.append("content hash mismatch: stored bytes do not hash to the registered sha256")
        if len(data) != art.size_bytes:
            problems.append("size mismatch")
        if provenance_hash(art) != art.provenance_hash:
            problems.append("provenance hash mismatch")
        for pid in art.parent_artifact_ids:
            try:
                self.repo.get_artifact(pid)
            except Exception:
                problems.append(f"parent artifact {pid} is missing")
        return VerifyResult(not problems, problems)

    def read(self, actor: Actor, artifact_id: str, for_task_id: str | None = None) -> bytes:
        """Authorize via the matrix FIRST (denials audited), then read and re-verify the hash before returning bytes."""
        art = self.repo.authorize_artifact_read(actor, artifact_id, for_task_id)
        data = self.store.read(art.sha256, final_eval=art.artifact_class is ArtifactClass.FINAL_EVALUATION)
        if sha256_hex(data) != art.sha256:
            raise IntegrityError(f"artifact {artifact_id}: stored bytes do not match the registered sha256")
        return data
