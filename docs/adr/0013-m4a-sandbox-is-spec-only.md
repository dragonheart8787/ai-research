# ADR-0013: M4a sandbox is spec-only; isolation is verified in M4b on the human's VM

- Status: Accepted
- Date: 2026-11-07

## Context
M4 was planned as "build the Docker sandbox and prove it". The build environment is a cloud workspace with a Docker **client** but **no daemon** (`docker
info`: cannot connect; `dockerd`/`runc`/`containerd` binaries exist but no daemon is running; uid 0; cgroup v2 `tmpfs` view). Starting a root daemon there
would not tell us anything about the human's rootless VM and was outside a read-only inspection.

## Decision
1. M4a builds and tests the parts that need no daemon: the run-spec generator and validator (`airos.sandbox.spec`), golden argv files, the egress policy
   as data with audited denials (`airos.sandbox.egress`, `config/egress.toml`), the container runner against a fake docker client
   (`airos.sandbox.runner`), the check-runner interface (`airos.sandbox.checks`), a real-clock heartbeat test, and a fail-closed live-check stub.
2. Every artefact of M4a that mentions isolation carries the label **"spec-only, isolation not verified"**. No test or document may claim a container
   property as verified until `docs/architecture/M4B-HANDOFF.md` has been run by the human and the result recorded.
3. Real-container tests are written (`tests/test_container_real.py`) and **skip** unless `AIROS_TEST_DOCKER=1` and a daemon socket exist; a skip is reported
   as SKIPPED, never as a pass.
4. Real adapters refuse in-process/host execution (runner kind must be `fixture` or `container`); checks never execute on the host; a `reproduce` check is
   `unverifiable` (= rejected) until a container check runner exists.
5. No provider token or login is requested, stored or used in the build environment; adapters stay disabled.

## Consequences
+ The policy surface (flags, mounts, env, networks, egress decisions) is reviewable and regression-tested now, and cheap to change.
- Until M4b, a bug in how Docker interprets a flag, a rootless-mapping problem, or a proxy that does not enforce the list would not be caught.
- The egress host names and CLI behaviour in the config are assumptions until the live check records the truth.
