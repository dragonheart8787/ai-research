# Adapter fixtures: SYNTHETIC, not live recordings

There are no fixture *files* for the provider CLIs any more: since ADR-0011 (subscription-only authentication) there is no HTTP transport, and the CLI
fixtures are built in code by `tests/real_helpers.py` (`claude_fx`, `codex_status_fx`, `codex_exec_fx`) and replayed by `FixtureCliRunner`.
Every one of them carries `"provenance": "synthetic: ... NOT a live recording"`.

What they prove: our adapters build the invocation we intend (argv, a minimal allow-listed environment, prompt over stdin, no token in argv) and
parse / validate the output shape we ASSUME fail-closed (success, usage limit, truncation, malformed, wrong auth path).

What they do NOT prove: that the real `claude` / `codex` CLIs accept these flags, emit these fields, name their credential-source field the way we
assume, or phrase a usage-limit message the way our patterns expect. All of that is recorded by the human-run live check on the VM
(`docs/architecture/M4B-HANDOFF.md`); a recording made there would carry `"provenance": "recorded from a live run ..."` so it stays distinguishable.
No recording exists yet. No test in CI runs a provider CLI, opens a socket, or starts a container.
