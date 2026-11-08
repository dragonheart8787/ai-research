"""Mock runners. A runner creates the per-run working directory. The WORKER proves (black box, see worker.py) that
every run gets a fresh one before workspace_write tasks may be treated as re-runnable; a runner's own claim is never trusted."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Runner(Protocol):
    def prepare(self, task_id: str, run_id: str) -> Path: ...


class FreshDirRunner:
    """One brand-new directory per run_id, created with an exclusive mkdir (fails if it already exists)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def prepare(self, task_id: str, run_id: str) -> Path:
        d = self.root / run_id
        os.mkdir(d)  # FileExistsError if reused: exclusivity is enforced by the OS, not by convention
        if any(d.iterdir()):
            raise RuntimeError("fresh run directory is not empty")
        return d


class SharedDirRunner:
    """Deliberately NOT fresh: all runs of a task share one directory, so leftovers from a crashed run persist."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def prepare(self, task_id: str, run_id: str) -> Path:
        d = self.root / task_id
        d.mkdir(exist_ok=True)
        return d
