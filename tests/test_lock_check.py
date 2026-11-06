from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_lock", ROOT / "scripts" / "check_lock.py")
assert _spec and _spec.loader
check_lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_lock)

LOCK = (ROOT / "requirements" / "dev.lock").read_text()


def _installed_from_lock() -> dict[str, str]:
    pins, _ = check_lock.parse_lock(LOCK)
    return dict(pins)


def test_real_environment_matches_lock():
    assert check_lock.compare(LOCK, check_lock.installed_packages()) == []


def test_every_lock_entry_has_hashes():
    pins, unhashed = check_lock.parse_lock(LOCK)
    assert len(pins) >= 10 and unhashed == []


def test_detects_version_drift_missing_and_extra_packages():
    inst = _installed_from_lock()
    name = next(iter(inst))
    drift = {**inst, name: "0.0.0"}
    assert any("version mismatch" in p for p in check_lock.compare(LOCK, drift))
    missing = {k: v for k, v in inst.items() if k != name}
    assert any("missing" in p for p in check_lock.compare(LOCK, missing))
    extra = {**inst, "requests": "2.0.0"}
    assert any("unlocked package installed: requests" in p for p in check_lock.compare(LOCK, extra))


def test_detects_unhashed_lock_entry():
    assert any("no sha256 hash" in p for p in check_lock.compare("foo==1.0\n", {"foo": "1.0"}))


def test_venv_is_not_tracked_by_git():
    import subprocess

    out = subprocess.run(["git", "ls-files", ".venv"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert out.strip() == ""
    assert ".venv/" in (ROOT / ".gitignore").read_text()
