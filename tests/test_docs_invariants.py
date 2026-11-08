"""Docs that carry safety decisions must keep saying them (cheap regression guard; not a substitute for the code tests)."""
from __future__ import annotations

from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _adr(n: str) -> str:
    return next((DOCS / "adr").glob(f"{n}-*.md")).read_text()


def test_adr_0005_records_the_scientist_threat_model_and_runbook():
    t = _adr("0005").lower()
    for needle in ("untrusted", "full tool permissions", "one provider host", "no host mounts", "docker.sock", "own credentials",
                   "wall-clock", "credit", "token-revocation runbook", "final-evaluation", "agsci", "rootless", "fail-closed stub",
                   "never shares a container", "env file"):
        assert needle in t, needle


def test_adr_0011_records_subscription_only_rules():
    t = _adr("0011").lower()
    for needle in ("never uses provider api keys", "subscription_oauth", "manual_handoff", "individual use", "no proxying", "no bridging",
                   "terms", "usage credits", "attestation", "revocation", "billable api key", "stays disabled"):
        assert needle in t, needle


def test_adr_0012_exists_and_proposal_points_at_the_auth_amendment():
    assert "scope deviation" in _adr("0012").lower()
    p = (DOCS / "architecture" / "PROPOSAL.md").read_text()
    assert "ADR-0011" in p and "WITHDRAWN" in p and "dedicated key with provider-side spend cap" not in p.split("Q6 |")[1][:200]


def test_no_active_doc_instructs_using_an_api_key():
    p = (DOCS / "architecture" / "PROPOSAL.md").read_text()
    assert "is a dedicated API key" not in p and "passed as a single env var to that container only. Egress-proxy" not in p


def test_adr_0004_says_author_metadata_is_forgeable_and_access_control_is_the_defense():
    text = _adr("0004").lower()
    assert "forgeable" in text and "repository access control" in text and "require_signature" in text and "default false" in text


def test_m4_docs_label_the_sandbox_spec_only_and_give_the_vm_procedure():
    arch = DOCS / "architecture"
    handoff = (arch / "M4B-HANDOFF.md").read_text()
    for needle in ("git clone", "git checkout claude/m4-sandbox", "requirements/dev.lock", "AIROS_LIVE=1", "AIROS_TEST_DOCKER=1", "snapshot",
                   "rootless", "dockerd-rootless-setuptool.sh", "exit=3", "do not log in"):
        assert needle.lower() in handoff.lower(), needle
    notes = (arch / "M4A-NOTES.md").read_text()
    assert "Verified by tests vs not verified" in notes and "NOT verified" in notes and "spec-only" in _adr("0013").lower()
    assert "isolation not verified" in (DOCS.parent / "tests" / "golden" / "sandbox" / "planner.txt").read_text()
