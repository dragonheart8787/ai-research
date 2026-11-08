"""The live contract check is a STUB in M4a and must fail closed: no success path, no token access, no network, no subprocess."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from airos.sandbox import live_check
from airos.sandbox.live_check import (
    EXIT_NOT_ENABLED,
    EXIT_NOT_IMPLEMENTED,
    LiveCheckNotImplemented,
    main,
    run_live_check,
)


def test_without_the_opt_in_it_refuses(monkeypatch, capsys):
    monkeypatch.delenv("AIROS_LIVE", raising=False)
    assert main([]) == EXIT_NOT_ENABLED != 0
    assert "AIROS_LIVE=1" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["1", "true", "yes", "0", ""])
def test_with_or_without_the_opt_in_it_never_succeeds(monkeypatch, value):
    monkeypatch.setenv("AIROS_LIVE", value)
    assert main([]) in (EXIT_NOT_ENABLED, EXIT_NOT_IMPLEMENTED) and main([]) != 0


def test_with_the_opt_in_it_says_not_implemented_and_fails_closed(monkeypatch, capsys):
    monkeypatch.setenv("AIROS_LIVE", "1")
    assert main([]) == EXIT_NOT_IMPLEMENTED
    err = capsys.readouterr().err
    assert "not implemented" in err and "nothing was verified" in err
    with pytest.raises(LiveCheckNotImplemented):
        run_live_check()


def test_it_never_touches_credentials_the_network_or_processes(monkeypatch):
    monkeypatch.setenv("AIROS_LIVE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-be-read")
    reads: list[str] = []
    import os

    real_get = os.environ.get
    monkeypatch.setattr(os.environ, "get", lambda k, d=None: (reads.append(k), real_get(k, d))[1])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("the stub spawned a process"))
    assert main([]) == EXIT_NOT_IMPLEMENTED                      # (the network guard in conftest already forbids sockets)
    assert reads == ["AIROS_LIVE"]


def test_the_module_has_no_success_exit_path():
    src = Path(live_check.__file__).read_text()
    assert "return 0" not in src and "SystemExit(0)" not in src and "sys.exit(0)" not in src
    assert sys.modules["airos.sandbox.live_check"] is live_check
