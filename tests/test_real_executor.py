"""Claude Agent SDK Executor adapter (against a scripted AgentLoop; no SDK, no container, no network).
The tool set is derived ONLY from the approved contract; the loop's reported behaviour is re-checked fail-closed."""
from __future__ import annotations

import pytest

from airos.agents.real.claude_executor import (
    ACTION_TO_TOOLS,
    ClaudeAgentSdkExecutorAdapter,
    LoopResult,
    ToolCall,
    tools_for,
)
from airos.agents.real.common import AdapterProtocolError
from airos.domain.schemas import ExpectedOutput, Role
from airos.redaction import Redactor
from tests.real_helpers import ANTHROPIC_EXEC_ENV, ENV, KEY_EXEC, ScriptedLoop, req

OUT = [ExpectedOutput(logical_name="result.md", media_type="text/markdown", artifact_class="work_product")]


def _adapter(loop, env=None, red=None):
    return ClaudeAgentSdkExecutorAdapter(model="claude-test", credential_env=ANTHROPIC_EXEC_ENV, environ=ENV if env is None else env,
                                         redactor=red or Redactor(), loop=loop)


def _writes(name="result.md", data=b"# result\n"):
    def w(spec):
        (spec.cwd / "out").mkdir(exist_ok=True)
        (spec.cwd / "out" / name).write_bytes(data)
    return w


def test_tools_are_derived_only_from_contract_actions_through_the_static_table():
    assert tools_for([]) == ()
    assert tools_for(["fs.read"]) == ("Glob", "Grep", "Read")
    assert tools_for(["fs.read", "fs.write.workspace"]) == ("Edit", "Glob", "Grep", "Read", "Write")
    # unknown / dangerous-sounding actions grant NOTHING (the table is the only source)
    assert tools_for(["shell.exec", "net.any", "Bash", "fs.write.anywhere", "FS.READ"]) == ()
    assert not ({"Bash", "WebFetch", "WebSearch", "Task"} & {t for ts in ACTION_TO_TOOLS.values() for t in ts})


def test_the_loop_receives_exactly_the_contract_tools_and_a_contract_prompt(tmp_path):
    loop = ScriptedLoop(LoopResult("done", files=["out/result.md"], tokens_in=40, tokens_out=9), writer=_writes())
    a = _adapter(loop)
    r = a.execute(req(Role.EXECUTOR, objective="OBJ-FROM-CONTRACT", allowed_actions=["fs.read", "fs.write.workspace"], outputs=OUT), tmp_path)
    [spec] = loop.specs
    assert spec.allowed_tools == ("Edit", "Glob", "Grep", "Read", "Write") and spec.cwd == tmp_path and spec.model == "claude-test"
    assert "OBJ-FROM-CONTRACT" in spec.prompt and spec.timeout_s > 0
    assert r.status == "succeeded" and not a.is_mock
    assert (r.resource_usage.tokens_in, r.resource_usage.tokens_out, r.resource_usage.model, r.resource_usage.usd_micro) == (40, 9, "claude-test", 0)
    [p] = r.produced_artifacts
    assert p.path == "out/result.md" and p.size_bytes == len(b"# result\n")


def test_a_tool_call_outside_the_contract_set_fails_closed_and_stores_nothing(tmp_path):
    loop = ScriptedLoop(LoopResult("done", tool_calls=[ToolCall("Read"), ToolCall("Bash", "rm -rf /")], files=["out/result.md"]), writer=_writes())
    with pytest.raises(AdapterProtocolError) as e:
        _adapter(loop).execute(req(Role.EXECUTOR, allowed_actions=["fs.read"], outputs=OUT), tmp_path)
    assert e.value.category == "policy" and "Bash" in e.value.detail


def test_with_no_allowed_actions_even_read_is_a_violation(tmp_path):
    loop = ScriptedLoop(LoopResult("done", tool_calls=[ToolCall("Read")]))
    assert loop.specs == []
    with pytest.raises(AdapterProtocolError, match="Read"):
        _adapter(loop).execute(req(Role.EXECUTOR, allowed_actions=[], outputs=OUT), tmp_path)
    assert loop.specs[0].allowed_tools == ()


@pytest.mark.parametrize("claimed", ["out/ghost.md", "../escape.md", "/etc/passwd", "result.md", "out/sub/result.md"])
def test_claimed_files_must_exist_be_safe_and_be_declared_outputs(tmp_path, claimed):
    loop = ScriptedLoop(LoopResult("done", files=[claimed]), writer=_writes())
    with pytest.raises(AdapterProtocolError) as e:
        _adapter(loop).execute(req(Role.EXECUTOR, allowed_actions=["fs.write.workspace"], outputs=OUT), tmp_path)
    assert e.value.category in ("missing_artifact", "policy")


def test_symlinked_output_is_rejected(tmp_path):
    import os

    def w(spec):
        (spec.cwd / "out").mkdir(exist_ok=True)
        secret = spec.cwd.parent / "host-secret.txt"
        secret.write_text("host data")
        os.symlink(secret, spec.cwd / "out" / "result.md")

    with pytest.raises(AdapterProtocolError) as e:
        _adapter(ScriptedLoop(LoopResult("d", files=["out/result.md"]), writer=w)).execute(
            req(Role.EXECUTOR, allowed_actions=["fs.write.workspace"], outputs=OUT), tmp_path)
    assert e.value.category == "missing_artifact"


def test_oversized_output_and_early_stops_are_rejected(tmp_path):
    small = [ExpectedOutput(logical_name="result.md", media_type="text/markdown", artifact_class="work_product", max_bytes=4)]
    with pytest.raises(AdapterProtocolError) as e:
        _adapter(ScriptedLoop(LoopResult("d", files=["out/result.md"]), writer=_writes(data=b"12345"))).execute(
            req(Role.EXECUTOR, allowed_actions=["fs.write.workspace"], outputs=small), tmp_path)
    assert e.value.category == "oversized"
    for stop in ("max_tokens", "max_turns"):
        with pytest.raises(AdapterProtocolError, match="truncated|stopped early"):
            _adapter(ScriptedLoop(LoopResult("d", stop_reason=stop))).execute(req(Role.EXECUTOR, outputs=OUT), tmp_path)


def test_secrets_in_text_output_are_redacted_before_hashing_and_binary_secrets_are_refused(tmp_path):
    leaked = f"token {KEY_EXEC} end".encode()
    r = _adapter(ScriptedLoop(LoopResult("d", files=["out/result.md"]), writer=_writes(data=leaked))).execute(
        req(Role.EXECUTOR, allowed_actions=["fs.write.workspace"], outputs=OUT), tmp_path)
    stored = (tmp_path / "out" / "result.md").read_bytes()
    assert KEY_EXEC.encode() not in stored and b"[REDACTED]" in stored
    import hashlib

    assert r.produced_artifacts[0].sha256 == hashlib.sha256(stored).hexdigest()       # the claim matches the REDACTED bytes
    binout = [ExpectedOutput(logical_name="blob.bin", media_type="application/octet-stream", artifact_class="work_product")]
    d2 = tmp_path / "second"
    d2.mkdir()
    with pytest.raises(AdapterProtocolError) as e:
        _adapter(ScriptedLoop(LoopResult("d", files=["out/blob.bin"]), writer=_writes("blob.bin", leaked))).execute(
            req(Role.EXECUTOR, allowed_actions=["fs.write.workspace"], outputs=binout), d2)
    assert e.value.category == "policy"


def test_missing_credentials_fail_closed_before_the_loop_runs(tmp_path):
    loop = ScriptedLoop()
    with pytest.raises(AdapterProtocolError) as e:
        _adapter(loop, env={}).execute(req(Role.EXECUTOR, outputs=OUT), tmp_path)
    assert e.value.category == "no_credentials" and loop.specs == []
