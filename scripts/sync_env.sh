#!/usr/bin/env bash
# Recreate/sync .venv EXACTLY to the hashed lockfile (hashes verified by uv), then verify.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3.11 -m venv .venv
uv pip sync --python .venv/bin/python requirements/dev.lock
.venv/bin/python scripts/check_lock.py requirements/dev.lock
