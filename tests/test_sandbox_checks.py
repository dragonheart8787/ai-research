"""Check runner: no execution on the host; unknown or un-runnable checks fail closed."""
from __future__ import annotations

import pytest

from airos.canonical import sha256_hex
from airos.domain.schemas import EvidenceCheck
from airos.sandbox.checks import (
    CheckItem,
    CheckOutcome,
    CheckRunnerRefused,
    FailClosedCheckRunner,
    FileCheckRunner,
    all_passed,
    assert_runner_allowed,
    run_checks,
)
from airos.verifier import verify_response
from tests.real_helpers import req


def _item(**kw):
    base = dict(check_id="c1", type="file_exists", target="out/a.txt")
    base.update(kw)
    return CheckItem(**base)


def test_file_checks_pass_and_fail_by_reading_files_only(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_bytes(b"hello")
    r = FileCheckRunner()
    assert r.run(_item(), tmp_path).passed
    assert r.run(_item(type="sha256_equals", expected=sha256_hex(b"hello")), tmp_path).passed
    bad = r.run(_item(type="sha256_equals", expected="0" * 64), tmp_path)
    assert bad.outcome is CheckOutcome.FAILED and "differs" in bad.detail
    assert r.run(_item(target="out/missing.txt"), tmp_path).outcome is CheckOutcome.FAILED


@pytest.mark.parametrize("target", ["../escape.txt", "/etc/passwd", "out/../../x"])
def test_unsafe_targets_fail(tmp_path, target):
    assert FileCheckRunner().run(_item(target=target), tmp_path).outcome is CheckOutcome.FAILED


@pytest.mark.parametrize("ctype", ["json_schema", "regex_in_artifact", "command_exit_zero", "metric_threshold", "anything_else", ""])
def test_unknown_or_unsupported_check_types_fail_closed(tmp_path, ctype):
    res = FileCheckRunner().run(_item(type=ctype), tmp_path)
    assert res.outcome is CheckOutcome.FAILED and "fail closed" in res.detail


def test_a_check_that_asks_for_re_execution_is_unverifiable_and_nothing_runs_on_the_host(tmp_path, monkeypatch):
    import subprocess

    marker = tmp_path / "ran"

    def boom(*a, **k):
        marker.write_text("executed")
        raise AssertionError("a check tried to execute something on the host")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("os.system", boom)
    res = FileCheckRunner().run(_item(type="command_exit_zero", reproduce=("touch", str(marker))), tmp_path)
    assert res.outcome is CheckOutcome.UNVERIFIABLE and not marker.exists()
    res2 = FileCheckRunner().run(_item(reproduce=("echo", "x")), tmp_path)             # even a "safe" type with reproduce is not executed
    assert res2.outcome is CheckOutcome.UNVERIFIABLE and not marker.exists()


def test_with_no_runner_configured_nothing_passes(tmp_path):
    assert FailClosedCheckRunner().run(_item(), tmp_path).outcome is CheckOutcome.UNVERIFIABLE


def test_a_host_executing_runner_is_structurally_refused():
    class HostExec:
        kind = "host"

        def run(self, check, workdir):  # pragma: no cover - must never be called
            raise AssertionError("must not run")

    with pytest.raises(CheckRunnerRefused, match="never execute on the host"):
        assert_runner_allowed(HostExec())   # type: ignore[arg-type]
    with pytest.raises(CheckRunnerRefused):
        run_checks(HostExec(), [_item()], None)   # type: ignore[arg-type]


def test_all_passed_requires_every_result_to_pass(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_bytes(b"x")
    results = run_checks(FileCheckRunner(), [_item(), _item(check_id="c2", type="mystery")], tmp_path)
    assert [r.passed for r in results] == [True, False] and not all_passed(results)
    assert all_passed(run_checks(FileCheckRunner(), [_item()], tmp_path)) and all_passed([])


def test_verifier_fails_a_response_whose_evidence_cannot_be_verified(tmp_path):
    from airos.domain.schemas import AcceptanceResult, AgentResponse, ResourceUsage

    request = req()
    usage = ResourceUsage(tokens_in=0, tokens_out=0, usd_micro=0, wall_ms=1, measured_by="adapter", model="mock")
    ev = [EvidenceCheck(check_id="e1", type="command_exit_zero", target="x", reproduce=["pytest", "-q"])]
    resp = AgentResponse(task_id=request.task_id, run_id=request.run_id, attempt=request.attempt, status="succeeded", resource_usage=usage,
                         evidence=ev, acceptance_results=[AcceptanceResult(criterion_id=c.id, self_reported="not_applicable")
                                                         for c in request.acceptance_criteria])
    verdict = verify_response(request, resp, tmp_path)
    assert any("evidence e1" in p and "unverifiable" in p for p in verdict.problems)
    assert not verdict.passed
