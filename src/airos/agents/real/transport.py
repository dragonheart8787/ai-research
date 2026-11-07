"""HTTP transport seam. Adapters never open sockets themselves; they call `Transport.send`.

* UrllibTransport: the production transport (https only). It is NEVER called by the test suite (a global guard blocks sockets).
* FixtureTransport: replays fixtures; any request that matches no fixture raises (strict). Used by every adapter test.
* RecordingTransport: wraps another transport and writes fixtures, for a human to capture real exchanges later.
Header values that look like credentials are masked in repr() and in fixtures.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from airos.errors import AirosError

SENSITIVE_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie", "set-cookie"})


class TransportError(AirosError):
    """Network-level failure (connection, TLS, timeout). Retryable by the caller's policy."""


class FixtureMismatch(AirosError):
    """No fixture matches the request (strict replay): the adapter's wire format drifted from the contract."""


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_s: float = 30.0

    def __repr__(self) -> str:  # never print credentials
        safe = {k: ("***" if k.lower() in SENSITIVE_HEADERS else v) for k, v in self.headers.items()}
        return f"HttpRequest({self.method} {self.url} headers={safe} body=<{len(self.body)} bytes>)"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class UrllibTransport:
    """Production transport. https only, no redirects followed, response size capped while reading."""

    def __init__(self, max_response_bytes: int = 4 * 1024 * 1024) -> None:
        self.max_response_bytes = max_response_bytes

    def send(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        if parts.scheme != "https" or not parts.hostname:
            raise TransportError(f"refusing non-https URL {request.url!r}")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a: Any, **k: Any) -> None:  # a redirect could leak the credential to another host
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(request.url, data=request.body or None, method=request.method, headers=dict(request.headers))
        try:
            with opener.open(req, timeout=request.timeout_s) as r:  # noqa: S310 - scheme checked above
                return HttpResponse(r.status, dict(r.headers), r.read(self.max_response_bytes + 1))
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, dict(exc.headers or {}), exc.read(self.max_response_bytes + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc


def _subset(want: Any, have: Any) -> bool:
    if isinstance(want, dict):
        return isinstance(have, dict) and all(k in have and _subset(v, have[k]) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) <= len(have) and all(_subset(w, h) for w, h in zip(want, have, strict=False))
    return bool(want == have)


@dataclass
class FixtureTransport:
    """Replays fixtures of the form
    {"request": {"method", "url", "headers_present": [...], "body_json_subset": {...}},
     "response": {"status", "headers", "body"}}   (body: JSON value, or {"raw": "<text>"} for non-JSON, or {"raw_b64"...})."""

    fixtures: list[dict[str, Any]]
    calls: list[HttpRequest] = field(default_factory=list)

    @classmethod
    def from_files(cls, *paths: str | Path) -> FixtureTransport:
        return cls([json.loads(Path(p).read_text(encoding="utf-8")) for p in paths])

    def send(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        try:
            body = json.loads(request.body.decode("utf-8")) if request.body else None
        except ValueError:
            body = None
        have = {k.lower() for k in request.headers}
        for fx in self.fixtures:
            want = fx["request"]
            if want["method"] != request.method or want["url"] != request.url:
                continue
            if not {h.lower() for h in want.get("headers_present", [])} <= have:
                continue
            if not _subset(want.get("body_json_subset", {}), body if body is not None else {}):
                continue
            resp = fx["response"]
            raw = resp["body"]
            data = raw["raw"].encode() if isinstance(raw, dict) and "raw" in raw else json.dumps(raw).encode()
            return HttpResponse(int(resp["status"]), dict(resp.get("headers", {})), data)
        raise FixtureMismatch(f"no fixture matches {request!r}")


@dataclass
class RecordingTransport:
    """Wraps a real transport; appends every exchange as a fixture file (credential headers are never written)."""

    inner: Transport
    out_dir: Path
    counter: int = 0

    def send(self, request: HttpRequest) -> HttpResponse:
        resp = self.inner.send(request)
        self.counter += 1
        try:
            parsed: Any = json.loads(resp.body.decode("utf-8"))
        except ValueError:
            parsed = {"raw": resp.body.decode("utf-8", "replace")}
        try:
            body_subset: Any = json.loads(request.body.decode("utf-8")) if request.body else {}
        except ValueError:
            body_subset = {}
        fx = {"name": f"recorded-{self.counter}", "provenance": "recorded from a live exchange by RecordingTransport",
              "request": {"method": request.method, "url": request.url,
                          "headers_present": sorted(h for h in request.headers), "body_json_subset": body_subset},
              "response": {"status": resp.status, "headers": {k: v for k, v in resp.headers.items() if k.lower() not in SENSITIVE_HEADERS},
                           "body": parsed}}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"recorded-{self.counter:03d}.json").write_text(json.dumps(fx, indent=2), encoding="utf-8")
        return resp
