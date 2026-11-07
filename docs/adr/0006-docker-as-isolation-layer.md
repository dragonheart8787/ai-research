# ADR-0006: Docker as the isolation layer

- Status: Proposed (M4a: the spec generator, egress policy and check-runner interface are implemented **spec-only, isolation not verified**; see ADR-0013)
- Date: 2026-11-06

## Context
The Executor runs agent-written code; the AI Scientist runs even more autonomous code. We need per-run filesystem, process, resource and network confinement, quick to adopt, reproducible, and testable. Observed environment: Docker client 29.9.2 installed, **no daemon running**, session is root, rootless status unknown. So container behaviour cannot be validated here until a host with a daemon is supplied.

## Decision
1. Use **Docker** (Engine API/CLI invoked only by `airos.sandbox`) as the isolation layer, with the hardening template in PROPOSAL §11.1: non-root uid 10001, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, seccomp (default → stricter later), cpu/mem/pids limits, noexec tmpfs, only scoped workspace mounts, internal network + allowlist proxy.
2. Prefer a **rootless Docker daemon or `userns-remap`** on the target host so container root maps to an unprivileged host user; never expose the Docker socket to any container or agent. If neither is available, M4 requires explicit human acknowledgement of the weaker posture.
3. The sandbox module builds run specs from a typed policy object, validates mounts against allow/deny lists (incl. final-evaluation data), and emits a golden-file-testable argument vector; no shell strings.
4. Image digests are pinned and recorded in `agents.image_digest`; runs refuse on digest drift.
5. Hardening path (ordered): gVisor (`runsc`) runtime → Kata/Firecracker VM → separate host. Selecting a stronger runtime must not change the policy object.
6. M1–M3 run with **no Docker dependency** (mock adapters); Docker-specific tests are tagged and run only on a host with a daemon.

## Consequences
+ Ubiquitous, scriptable, resource limits and networks are first-class; spec is testable offline (golden files) even without a daemon.
− Shared host kernel: not a security boundary against kernel exploits; the Docker daemon (root) is a high-value target — whoever can talk to it is root; therefore only the sandbox module may.
− Egress allowlisting by hostname needs a proxy (Docker lacks FQDN firewalling) — extra component, TLS-CONNECT-level filtering only.
− Rootless mode limits some features (e.g., certain network modes, performance) — acceptable for CPU workloads.
− This environment cannot exercise it; risk #1.

## Alternatives
- **Podman rootless:** better default rootless story and daemonless; very similar CLI. Reasonable substitute; kept behind the same policy object, not chosen first because the user environment already ships Docker.
- **gVisor/Kata/Firecracker directly:** stronger isolation, higher setup cost; adopt as hardening (item 5).
- **Linux namespaces/bubblewrap/nsjail by hand:** minimal, but we would own security-critical plumbing and portability — rejected (see ADR-0007).
- **No sandbox, restricted Python / subprocess:** insufficient isolation for arbitrary code; rejected.
- **Cloud sandboxes (E2B, Modal, etc.):** strong and managed, but adds external dependency, credentials and data egress; revisit for GPU later.
