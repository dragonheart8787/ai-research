"""Egress allow-list as data: POLICY LOGIC ONLY. Nothing here blocks a packet; the enforcing proxy is M4b (needs the VM)."""
from __future__ import annotations

from pathlib import Path

import pytest

from airos.clock import FakeClock
from airos.sandbox.egress import (
    MAX_HOSTS_PER_ROLE,
    EgressAuditor,
    EgressConfigError,
    load_egress_policy,
    normalize_host,
    parse_egress_policy,
)

CONFIG = Path(__file__).resolve().parent.parent / "config" / "egress.toml"
POL = load_egress_policy(CONFIG)


def test_shipped_policy_is_default_deny_with_only_https_and_a_few_exact_hosts():
    assert POL.ports == frozenset({443})
    assert POL.decide("planner", "api.anthropic.com").allowed
    assert POL.decide("executor", "api.anthropic.com").allowed
    assert POL.decide("director", "chatgpt.com").allowed
    assert not POL.decide("planner", "chatgpt.com").allowed and not POL.decide("director", "api.anthropic.com").allowed   # per role
    for role in ("chatgpt_work", "ai_scientist"):
        assert POL.hosts[role] == frozenset()
        assert not POL.decide(role, "api.anthropic.com").allowed
    assert all(len(h) <= MAX_HOSTS_PER_ROLE for h in POL.hosts.values())
    assert "ASSUMED" in CONFIG.read_text() and "UNVERIFIED" in CONFIG.read_text()          # the file says these hosts are unconfirmed


@pytest.mark.parametrize("role,host,port", [
    ("planner", "evil.example", 443), ("planner", "api.anthropic.com", 80), ("planner", "api.anthropic.com", 8443),
    ("planner", "sub.api.anthropic.com", 443), ("planner", "anthropic.com", 443), ("planner", "xapi.anthropic.com", 443),
    ("planner", "api.anthropic.com.evil.example", 443), ("planner", "1.2.3.4", 443), ("planner", "::1", 443), ("planner", "[::1]", 443),
    ("planner", "127.0.0.1", 443), ("planner", "localhost", 443), ("planner", "", 443), ("planner", "https://api.anthropic.com", 443),
    ("planner", "api.anthropic.com/x", 443), ("planner", "user@api.anthropic.com", 443), ("planner", "api.anthropic.com:443", 443),
    ("planner", "*.anthropic.com", 443), ("planner", " ", 443),
    ("planner", "apı.anthropic.com", 443), ("wizard", "api.anthropic.com", 443), ("", "api.anthropic.com", 443),
    ("Planner", "api.anthropic.com", 443),
])
def test_everything_not_exactly_allow_listed_is_denied(role, host, port):
    d = POL.decide(role, host, port)
    assert d.allowed is False and d.reason


@pytest.mark.parametrize("host", ["API.Anthropic.COM", "api.anthropic.com.", " api.anthropic.com "])
def test_host_case_trailing_dot_and_whitespace_are_normalised_not_widened(host):
    assert POL.decide("planner", host).allowed and POL.decide("planner", host).host == "api.anthropic.com"


def test_normalize_host_rejects_what_is_not_a_plain_dns_name():
    for bad in ("1.2.3.4", "::1", "a_b.example.com", "exa mple.com", "a..b.com", "-a.example.com", "a.b", "x" * 300 + ".com", "*.com", "a/b.com"):
        assert normalize_host(bad) is None, bad
    assert normalize_host("Example.COM.") == "example.com"


def test_unknown_role_and_empty_policy_deny_everything():
    empty = parse_egress_policy({"policy": {"default": "deny"}})
    for role in ("planner", "executor", "director", "chatgpt_work", "ai_scientist", "nope"):
        assert not empty.decide(role, "api.anthropic.com").allowed


@pytest.mark.parametrize("bad", [
    {"policy": {"default": "allow"}}, {}, {"policy": {}}, {"policy": {"default": "deny", "ports": []}},
    {"policy": {"default": "deny", "ports": [0]}}, {"policy": {"default": "deny", "ports": [True]}},
    {"policy": {"default": "deny"}, "roles": {"planner": {"hosts": ["*.anthropic.com"]}}},
    {"policy": {"default": "deny"}, "roles": {"planner": {"hosts": ["1.2.3.4"]}}},
    {"policy": {"default": "deny"}, "roles": {"planner": {"hosts": ["https://api.anthropic.com"]}}},
    {"policy": {"default": "deny"}, "roles": {"planner": {"hosts": ["API.anthropic.com"]}}},
    {"policy": {"default": "deny"}, "roles": {"planner": {"hosts": [f"h{i}.example.com" for i in range(MAX_HOSTS_PER_ROLE + 1)]}}},
    {"policy": {"default": "deny"}, "roles": {"wizard": {"hosts": []}}},
    {"policy": {"default": "deny"}, "roles": {"chatgpt_work": {"hosts": ["a.example.com"]}}},
    {"policy": {"default": "deny"}, "roles": {"planner": {"hosts": [], "extra": 1}}},
    {"policy": {"default": "deny"}, "extra": {}},
])
def test_a_malformed_or_widening_policy_is_refused(bad):
    with pytest.raises(EgressConfigError):
        parse_egress_policy(bad)


def test_invalid_toml_is_refused():
    with pytest.raises(EgressConfigError, match="invalid TOML"):
        load_egress_policy("[policy\n")


def test_proxy_allowlist_text_is_sorted_exact_and_empty_for_unlisted_roles():
    assert POL.proxy_allowlist("director") == "auth.openai.com\nchatgpt.com\n"
    assert POL.proxy_allowlist("planner") == "api.anthropic.com\n" and POL.proxy_allowlist("ai_scientist") == "" and POL.proxy_allowlist("zzz") == ""


# ------------------------------------------------------------------ denials are audited (deduplicated)


def test_denials_are_audited_with_dedupe_and_allows_are_not(repo):
    clock = FakeClock()
    aud = EgressAuditor(repo, clock, window_ms=60_000)
    deny = POL.decide("planner", "evil.example", 443)
    assert aud.record(POL.decide("planner", "api.anthropic.com")) is False                    # an allow is never a denial row
    assert aud.record(deny) is True
    assert aud.record(deny) is False and aud.record(deny) is False                            # a retry loop does not flood the log
    clock.advance(60_000)
    assert aud.record(deny) is True
    rows = [e for e in repo.iter_events() if e.event_type == "egress.denied"]
    assert len(rows) == 2 and rows[0].payload["host"] == "evil.example" and rows[0].payload["suppressed_since_last"] == 0
    assert rows[1].payload["suppressed_since_last"] == 2 and rows[1].payload["role"] == "planner" and rows[1].payload["port"] == 443
    assert aud.record(POL.decide("director", "evil.example")) is True                         # a different key is its own row
    assert repo.verify_chain().ok


def test_the_audited_host_is_bounded_and_carries_no_credentials(repo):
    aud = EgressAuditor(repo, FakeClock())
    d = POL.decide("planner", "x" * 400 + ".example.com")
    assert aud.record(d)
    [row] = [e for e in repo.iter_events() if e.event_type == "egress.denied"]
    assert len(row.payload["host"]) <= 253
