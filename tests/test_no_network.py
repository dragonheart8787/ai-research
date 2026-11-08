"""CI makes ZERO network calls. The suite-wide guard (conftest) turns any connect/resolve into a failing assertion, and the
production tree contains no raw HTTP transport at all (ADR-0011: no API keys, so no HTTP client to a provider)."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "airos"


def test_the_guard_blocks_connecting_and_name_resolution():
    with pytest.raises(AssertionError, match="network access is forbidden"):
        socket.create_connection(("example.com", 443))
    with pytest.raises(AssertionError, match="network access is forbidden"):
        socket.getaddrinfo("api.openai.com", 443)
    with socket.socket() as s, pytest.raises(AssertionError, match="network access is forbidden"):
        s.connect(("127.0.0.1", 9))


def test_no_source_module_opens_sockets_or_speaks_http():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if any(w in p.read_text() for w in ("import socket", "urllib.request", "http.client", "import requests", "import httpx",
                                                     "UrllibTransport"))]
    assert offenders == []


def test_the_raw_http_transport_module_is_gone():
    assert not (SRC / "agents" / "real" / "transport.py").exists()
    assert not (SRC / "agents" / "real" / "llm_adapters.py").exists()
