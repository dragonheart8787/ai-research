"""The M4b live contract check. NOT IMPLEMENTED in M4a, and deliberately FAIL-CLOSED: it can never report success.

M4b (human, on the VM, `AIROS_LIVE=1`) will replace the body of `run_live_check` with checks that need a real Docker daemon and the human's own
subscription login: CLI flag/output shapes, the credential-source field, usage-limit wording, actual egress hosts, rootless status, container
confinement. Until then this stub refuses. It never reads, asks for or uses any token or login.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Sequence

EXIT_NOT_ENABLED = 2
EXIT_NOT_IMPLEMENTED = 3


class LiveCheckNotImplemented(RuntimeError):
    pass


def run_live_check() -> None:
    raise LiveCheckNotImplemented("the live contract check is not implemented in M4a (it is M4b work on the VM); nothing was verified")


def main(argv: Sequence[str] | None = None) -> int:
    if os.environ.get("AIROS_LIVE") != "1":
        print("refusing: set AIROS_LIVE=1 to run the live contract check (it is never run in CI).", file=sys.stderr)
        return EXIT_NOT_ENABLED
    try:
        run_live_check()
    except LiveCheckNotImplemented as exc:
        print(f"FAIL (closed): {exc}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED
    return EXIT_NOT_IMPLEMENTED   # unreachable by design: there is no success path


if __name__ == "__main__":
    raise SystemExit(main())
