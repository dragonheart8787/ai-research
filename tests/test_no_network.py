"""CI makes ZERO network calls. The suite-wide guard (conftest) turns any connect/resolve into a failing assertion."""
from __future__ import annotations

import json
import socket

import pytest

from airos.agents.real.transport import (
    FixtureMismatch,
    FixtureTransport,
    HttpRequest,
    RecordingTransport,
    TransportError,
    UrllibTransport,
)


def test_the_guard_blocks_connecting_and_name_resolution():
    with pytest.raises(AssertionError, match="network access is forbidden"):
        socket.create_connection(("example.com", 443))
    with pytest.raises(AssertionError, match="network access is forbidden"):
        socket.getaddrinfo("api.openai.com", 443)
    with socket.socket() as s, pytest.raises(AssertionError, match="network access is forbidden"):
        s.connect(("127.0.0.1", 9))


def test_the_production_transport_cannot_reach_anything_from_a_test():
    t = UrllibTransport()
    with pytest.raises(AssertionError, match="network access is forbidden"):
        t.send(HttpRequest("POST", "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"}, b"{}"))
    for bad in ("http://api.openai.com/v1", "ftp://x/y", "file:///etc/passwd", "https:///nohost"):
        with pytest.raises(TransportError, match="refusing non-https"):
            t.send(HttpRequest("GET", bad, {}, b""))


def test_fixture_transport_is_strict():
    ft = FixtureTransport([{"request": {"method": "POST", "url": "https://a.example/x", "headers_present": ["X-K"], "body_json_subset": {"m": 1}},
                            "response": {"status": 200, "headers": {}, "body": {"ok": True}}}])
    assert json.loads(ft.send(HttpRequest("POST", "https://a.example/x", {"x-k": "v"}, b'{"m":1,"z":2}')).body) == {"ok": True}
    for req in (HttpRequest("GET", "https://a.example/x", {"x-k": "v"}, b'{"m":1}'), HttpRequest("POST", "https://a.example/y", {"x-k": "v"}, b'{"m":1}'),
                HttpRequest("POST", "https://a.example/x", {}, b'{"m":1}'), HttpRequest("POST", "https://a.example/x", {"x-k": "v"}, b'{"m":2}'),
                HttpRequest("POST", "https://a.example/x", {"x-k": "v"}, b"not json")):
        with pytest.raises(FixtureMismatch):
            ft.send(req)


def test_recording_transport_writes_fixtures_without_credentials(tmp_path):
    inner = FixtureTransport([{"request": {"method": "POST", "url": "https://a.example/x", "headers_present": [], "body_json_subset": {}},
                               "response": {"status": 200, "headers": {"Set-Cookie": "secret", "content-type": "application/json"}, "body": {"a": 1}}}])
    rt = RecordingTransport(inner, tmp_path / "rec")
    rt.send(HttpRequest("POST", "https://a.example/x", {"Authorization": "Bearer SUPER-SECRET-TOKEN"}, b'{"q":1}'))
    [f] = list((tmp_path / "rec").glob("*.json"))
    text = f.read_text()
    assert "SUPER-SECRET-TOKEN" not in text and "Set-Cookie" not in text and '"secret"' not in text
    doc = json.loads(text)
    assert doc["provenance"].startswith("recorded from a live exchange") and doc["request"]["headers_present"] == ["Authorization"]


def test_no_source_module_opens_sockets_outside_the_one_production_transport():
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "airos"
    offenders = [str(p.relative_to(src)) for p in src.rglob("*.py")
                 if any(w in p.read_text() for w in ("import socket", "urllib.request", "http.client", "import requests", "import httpx"))]
    assert offenders == ["agents/real/transport.py"]
