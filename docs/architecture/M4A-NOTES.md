# M4a notes (branch `claude/m4-sandbox`)

Scope reminder: Part A (M3 fixes + subscription-only authentication) is complete on `claude/m3-integrations`; Part B here is **M4a only**. Everything that
needs a Docker daemon is deferred to M4b (`M4B-HANDOFF.md`).

## Step 0 - environment inspection (read-only), recorded
| Item | Observed |
|---|---|
| OS / kernel | Ubuntu 24.04.5, Linux 6.18.44, x86_64, uid 0 |
| Docker client | 29.9.2 |
| Docker daemon | **none**: `failed to connect to the docker API at unix:///var/run/docker.sock ... no such file`; no `/run/user/*/docker.sock` |
| Binaries present | `dockerd`, `containerd`, `runc`; not present: `podman`, `nerdctl`, `rootlesskit`, `newuidmap` |
| User namespaces | `max_user_namespaces=64267`; `/etc/subuid` has only `ubuntu` |
| Rootless status | unknown / not applicable (no daemon) |
Per the environment override this did not stop M4a. No daemon was started.

## What was built
| Piece | Where | Notes |
|---|---|---|
| Run-spec generator + validator + `docker run` argv renderer | `src/airos/sandbox/spec.py` | PROPOSAL 11.1 flags; refuses invalid specs; token travels by NAME only |
| Golden files | `tests/golden/sandbox/{planner,executor,director}.txt` | headed "spec-only, isolation not verified" |
| Egress allow-list as data, default deny, audited denials | `config/egress.toml`, `src/airos/sandbox/egress.py` | host names are **assumptions** |
| Container runner (kind `container`) + `DockerClient` seam | `src/airos/sandbox/runner.py` | tested only with a fake client; `SubprocessDockerClient` never runs in CI |
| Check-runner interface, fail-closed | `src/airos/sandbox/checks.py`, `src/airos/verifier.py` | file checks only; `reproduce` => unverifiable; unknown type => failed; host runner refused |
| Real adapters refuse in-process execution | `agents/real/common.py`, worker gate (existing from Part A), tests in `test_sandbox_runner.py` | |
| Real-clock heartbeat test | `tests/test_heartbeat_realclock.py` | ~3 s; includes a no-heartbeat control that must lose its lease |
| Real-container tests | `tests/test_container_real.py` | 6 tests, **SKIPPED here** |
| Live contract check | `src/airos/sandbox/live_check.py` | stub, exit 2/3, no success path |
| Hand-off | `M4B-HANDOFF.md`, `docker/test-image/Dockerfile` | |

## Changes outside `sandbox/`
`CliInvocation` gained `role` and `run_id` (the container runner needs them); `verify_response` now evaluates acceptance checks **and response evidence**
through a `CheckRunner` (before, evidence was ignored and unsupported criteria failed inline; the outcome for unsupported types is unchanged). `pyproject.toml`
registers the `docker_real` marker.

## Verified by tests vs not verified
| Verified by tests (run here: 708 passed, 6 skipped) | NOT verified / needs the VM (M4b) |
|---|---|
| The generator emits exactly the PROPOSAL 11.1 flags (golden files) and refuses privileged, cap-add, host/default/shared network, extra or out-of-tree mounts, docker.sock, protected-data overlap (incl. via symlink), unpinned images, shell commands, forbidden/secret-shaped env, unsafe run ids | That Docker honours any of those flags: uid 10001, read-only rootfs, dropped caps, no-new-privileges, pids/memory/cpu limits, tmpfs `noexec` |
| A token never appears in an argv, a spec repr or a golden file; it reaches only the (fake) docker client environment, by name | That the real docker client forwards it only into the intended container and that it is absent from `docker inspect`/process listings |
| Roles share no network, login volume, host credential variable or proxy name (spec level); only the Director has a login volume | That volumes/networks really are separate on a daemon; the Director login volume contents |
| Egress decisions: default deny, exact host, port 443 only, no wildcards/IPs/URLs, per role; malformed or widening config refused; denials audited and de-duplicated | **Any actual blocking**: the allow-list proxy does not exist yet; whether the assumed host names are the real ones |
| Container runner builds specs, creates run dirs, kills on timeout, refuses unsandboxable invocations; Planner and Director adapters work through it with a fake docker | That a container starts, runs the CLI, and is removed; rootless directory ownership/mapping |
| Real adapters refuse host/in-process runners; the only process-spawning modules are the three expected ones | Anything about the Executor Agent SDK loop in a container (not built) |
| Checks never execute on the host; `reproduce` checks are unverifiable; unknown types fail; evidence is verified | A container-backed check runner |
| A slow adapter keeps its lease because the worker really heartbeats (real clock), and the same run without heartbeats is recovered | Heartbeat behaviour under a real container, process kill or machine suspend |
| The live check cannot succeed and reads no credentials | The live check itself; CLI flags/output shapes, credential-source field, usage-limit wording, `codex login status` semantics |
| Part A items (see the M3 report) | That "usage credits" are OFF, the Pro credit amount, provider terms: human attestations/verification only |

## Part A coverage map (what existed before vs new)
Pre-existing tests, migrated to the subscription model: `test_secrets_in_adapters`, `test_prompt_injection`, `test_real_executor`,
`test_real_adapters_matrices`, `test_handoff_git` (signature/author tests already existed), `test_budget`, `test_authz`, `test_pricing`,
`test_registry_routing`, `test_adapter_response_validation`, `test_no_network`. **Deleted with the HTTP transports:** `test_real_llm_adapters` and the
openai/anthropic fixtures. **New:** `test_cli_adapters`, `test_auth_env_config`, `test_credit_billing_orphans`, the redaction/login-pattern test, the
`require_signature` switch test, the ADR-0004 docs test, and all `test_sandbox_*`, `test_heartbeat_realclock`, `test_container_real`, `test_live_check_stub`.

## Open questions for the human
1. Confirm the Pro plan's Agent SDK credit amount (we plan with 15 USD of an assumed 20) and that "usage credits" are OFF.
2. Rootless vs `userns-remap` on the VM (this file assumes rootless).
3. Are the assumed egress hosts acceptable as the starting list for the live check to confirm or correct?
4. Directory ownership under rootless Docker: prefer a per-run `chmod`, a helper container, or Podman's `keep-id`? (decide after seeing the M4b result.)

## Status table update (after the lost M4b Part 1; see M4B-DESIGN-RECORD.md and ADR-0014)
Entries above for M1-M3 and M4a are unchanged. These items move or stay as follows:

| Item | Status in this repository |
|---|---|
| Container isolation (flags honoured, rootless behaviour, limits, volumes) | **Not verified in this repository (historical results only).** Reported results from a lost session are in M4B-DESIGN-RECORD.md. |
| Egress enforcement (allow-list proxy) | **Not verified in this repository (historical results only).** |
| Check runner (container-backed) | **Not verified in this repository (historical results only).** |
| Hostile workloads | **Not verified in this repository (historical results only).** |
| Real CLI behaviour (flags, output shapes, credential-source field, usage-limit wording, login status) | **Not verified.** |
