"""Egress allow-list as data, with default deny. This module holds only the POLICY LOGIC and its audit trail; the proxy that would enforce it
on a real network is M4b. Tests here prove the decisions, not that any packet is blocked."""
from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from airos.clock import Clock, SystemClock
from airos.errors import AirosError
from airos.persistence.sqlite_repo import SqliteRepository

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_HOSTS_PER_ROLE = 4
KNOWN_ROLES = frozenset({"director", "planner", "executor", "chatgpt_work", "ai_scientist"})


class EgressConfigError(AirosError):
    pass


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    role: str
    host: str
    port: int
    reason: str


def normalize_host(raw: str) -> str | None:
    """Canonical lower-case DNS name, or None if it is not an acceptable plain host name (IP literals, URLs, wildcards, userinfo, ports...)."""
    h = raw.strip().lower().rstrip(".")
    if not h or not h.isascii() or any(c in h for c in "/:@*?#%[]\\ \t\n_"):
        return None
    try:
        ipaddress.ip_address(h)
        return None                        # IP literals are never allow-listable
    except ValueError:
        pass
    return h if HOST_RE.fullmatch(h) else None


@dataclass(frozen=True)
class EgressPolicy:
    ports: frozenset[int]
    hosts: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def decide(self, role: str, host: str, port: int = 443) -> EgressDecision:
        h = normalize_host(host)
        if role not in KNOWN_ROLES:
            return EgressDecision(False, role, host, port, "unknown role: default deny")
        if h is None:
            return EgressDecision(False, role, host, port, "not an acceptable plain DNS host name (IP literal, wildcard, URL or malformed)")
        if port not in self.ports:
            return EgressDecision(False, role, h, port, f"port {port} is not allowed")
        if h not in self.hosts.get(role, frozenset()):
            return EgressDecision(False, role, h, port, "host is not on this role's allow-list: default deny")
        return EgressDecision(True, role, h, port, "allow-listed")

    def proxy_allowlist(self, role: str) -> str:
        """The text a per-role allow-list proxy would be configured with (sorted, one host per line)."""
        return "".join(f"{h}\n" for h in sorted(self.hosts.get(role, frozenset())))


def parse_egress_policy(data: Mapping[str, Any]) -> EgressPolicy:
    extra = set(data) - {"policy", "roles"}
    if extra:
        raise EgressConfigError(f"unknown key(s) {sorted(extra)}")
    pol = data.get("policy", {})
    if set(pol) - {"default", "ports"} or pol.get("default") != "deny":
        raise EgressConfigError('[policy] must contain default = "deny" (and optionally ports)')
    ports = pol.get("ports", [443])
    if not isinstance(ports, list) or not ports or not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in ports):
        raise EgressConfigError("[policy].ports must be a non-empty list of valid port numbers")
    roles = data.get("roles", {})
    hosts: dict[str, frozenset[str]] = {}
    for role, body in roles.items():
        if role not in KNOWN_ROLES:
            raise EgressConfigError(f"unknown role {role!r}")
        if set(body) - {"hosts"}:
            raise EgressConfigError(f"unknown key(s) in [roles.{role}]")
        raw = body.get("hosts", [])
        if not isinstance(raw, list) or len(raw) > MAX_HOSTS_PER_ROLE:
            raise EgressConfigError(f"[roles.{role}].hosts must be a list of at most {MAX_HOSTS_PER_ROLE} host names")
        norm: set[str] = set()
        for h in raw:
            n = normalize_host(h) if isinstance(h, str) else None
            if n is None or n != h:
                raise EgressConfigError(f"[roles.{role}] host {h!r}: only exact lower-case DNS names are allowed (no wildcards, IPs, URLs)")
            norm.add(n)
        if role in ("chatgpt_work",) and norm:
            raise EgressConfigError("chatgpt_work is a manual handoff: it can have no egress")
        hosts[role] = frozenset(norm)
    return EgressPolicy(frozenset(ports), hosts)


def load_egress_policy(source: str | Path) -> EgressPolicy:
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    try:
        return parse_egress_policy(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise EgressConfigError(f"invalid TOML: {exc}") from exc


class EgressAuditor:
    """Writes `egress.denied` audit rows. The same (role, host, port) is collapsed inside a window so a retry loop cannot flood the log;
    the suppressed count is carried on the next row."""

    def __init__(self, repo: SqliteRepository, clock: Clock | None = None, window_ms: int = 60_000) -> None:
        self._repo, self._clock, self._window = repo, clock or SystemClock(), window_ms
        self._last: dict[tuple[str, str, int], tuple[int, int]] = {}      # key -> (last_audited_ms, suppressed_since)

    def record(self, decision: EgressDecision, task_id: str | None = None) -> bool:
        """Audit a DENIAL. Returns True if a row was written. Allowed decisions are never audited here."""
        if decision.allowed:
            return False
        now = self._clock.now_ms()
        key = (decision.role, decision.host, decision.port)
        last, suppressed = self._last.get(key, (-self._window, 0))
        if now - last < self._window:
            self._last[key] = (last, suppressed + 1)
            return False
        self._repo.record_system_event("egress.denied", {"role": decision.role, "host": decision.host[:253], "port": decision.port,
                                                         "reason": decision.reason, "suppressed_since_last": suppressed}, task_id)
        self._last[key] = (now, 0)
        return True
