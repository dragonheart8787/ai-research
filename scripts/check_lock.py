#!/usr/bin/env python3
"""Fail (exit 1) if the installed packages differ from the hashed lockfile.

Usage: python scripts/check_lock.py [requirements/dev.lock]
Run with the interpreter of the environment being checked (e.g. .venv/bin/python).
Checks: every pin is installed at exactly the locked version; nothing else is installed except bootstrap
tooling; every pin carries at least one sha256 hash in the lockfile. (Hashes themselves are verified at
install time by `uv pip sync` / `pip install --require-hashes`; installed files cannot be re-hashed here.)
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

BOOTSTRAP = {"pip", "setuptools", "wheel", "pkg-resources"}
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\;]+)", re.M)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> tuple[dict[str, str], list[str]]:
    """Return ({name: version}, [names without a hash])."""
    pins: dict[str, str] = {}
    unhashed: list[str] = []
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9])", text)
    for block in blocks:
        m = _PIN.match(block)
        if not m:
            continue
        name = norm(m.group(1))
        pins[name] = m.group(2)
        if "--hash=sha256:" not in block:
            unhashed.append(name)
    return pins, unhashed


def compare(lock_text: str, installed: dict[str, str]) -> list[str]:
    pins, unhashed = parse_lock(lock_text)
    problems = [f"lock entry has no sha256 hash: {n}" for n in unhashed]
    for name, ver in sorted(pins.items()):
        have = installed.get(name)
        if have is None:
            problems.append(f"missing: {name}=={ver} is locked but not installed")
        elif have != ver:
            problems.append(f"version mismatch: {name} installed {have}, locked {ver}")
    for name, ver in sorted(installed.items()):
        if name not in pins and name not in BOOTSTRAP:
            problems.append(f"unlocked package installed: {name}=={ver}")
    return problems


def installed_packages() -> dict[str, str]:
    return {norm(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}


def main(argv: list[str]) -> int:
    lock = Path(argv[1] if len(argv) > 1 else "requirements/dev.lock")
    problems = compare(lock.read_text(encoding="utf-8"), installed_packages())
    for p in problems:
        print(f"LOCK CHECK FAILED: {p}", file=sys.stderr)
    if not problems:
        print(f"lock check ok: installed packages match {lock}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
