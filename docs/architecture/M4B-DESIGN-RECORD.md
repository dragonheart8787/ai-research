# M4b design record (historical, unverified)

> **Provenance warning.** Everything below was **reported during a session whose code and environment were lost; not reproducible; not verified in this
> repository.** An M4b Part 1 implementation (about a dozen commits, roughly 1100 tests, run against real rootless Docker on a developer's WSL2 machine)
> was never pushed, and its environment was permanently deleted. No code, test or log from it exists here. This file records the reported findings and
> design decisions so they are not lost; none of it is verified status, and no test in this repository exercises any of it. Treat each statement as a
> lead to re-check, not as a fact. See ADR-0014 for the loss and the deferral.

## a. Clock behaviour on WSL2
- Reported: the WSL wall clock jumped about +7.9 s and back about 0.2 s later, roughly every 5 s, until the Windows host clock (about 7.9 s off against a
  public NTP server; time source "Local CMOS Clock") was corrected and WSL was restarted. Stopping `systemd-timesyncd` did not help.
- Cause: supported by this evidence, **not proven**.
- Reported: the monotonic clock ran about 4 % fast relative to `MONOTONIC_RAW`.
- Design adopted: lease TTL at least 2 s **and** at least 10x the heartbeat interval; heartbeat scheduling on the monotonic clock; persisted lease expiry
  on the wall clock.
- Designed but **never implemented**: a clock-divergence guard (each heartbeat, compare the wall-clock delta with the monotonic delta; on divergence pause
  expiry decisions, write a `clock_anomaly` audit event, and never re-run side-effectful tasks).

## b. WSL hardening
- The Windows-interop binfmt entry (`WSLInterop`) was reported registered despite `interop=false` in `wsl.conf`. It was removed by writing `-1` to
  `/proc/sys/fs/binfmt_misc/WSLInterop`, plus a systemd oneshot unit at boot.
- WSLg was disabled with `guiApplications=false`.
- WSL remains a weaker boundary than a dedicated VM (shared kernel; a read-only 9p drivers share stays).
- A `/mnt/wslg` directory chain (`run/user/<uid>`, root, mode 0700) remained and needed a human attestation.

## c. Rootless Docker setup and findings
- The system `docker.service`, `docker.socket` and `containerd.service` had to be disabled **and masked** so that no root daemon exists. The user must not
  be in the `docker` group.
- cgroup v2 delegation was needed for memory and pids limits (reported as verified with a 64 MiB / 32 pid probe).
- Quirks reported:
  - copying image content into a new volume needs `volume-nocopy`;
  - `docker cp --archive` changes owners to the helper container's user;
  - on an internal network Docker's resolver forwards external lookups to the host resolver, so agent containers use `--dns 127.0.0.1`;
  - `/proc/self/mountinfo` inside a container reveals the host username and the Docker data path;
  - `--init` mounts the host's `docker-init` read-only.
- Host bind mounts failed under rootless Docker (permission denied). The design replaced them with **one fresh named volume per run**, with outputs read
  out through a short-lived read-only container. (This supersedes the bind-mount layout in the M4a specs and answers M4a open question 4 only as a report.)

## d. Test-design lessons
- Two container tests passed without any container starting, because they only asserted a non-zero exit.
- Rules adopted: every negative test needs a **positive control** (container proven started, then a *specific* error asserted); plus a **meta-test** that
  fails if a container test passes with a dead daemon.
- The audit log refused `sandbox.*` and `egress.*` events until a test checked every emittable event type.

## e. OOM detection
- Docker's `OOMKilled` flag was missed in 6 of 162 runs, and in 4 of 50 later runs, while the kernel did OOM-kill inside the cgroup. Cause not established.
- Oracle design: exit code 137 **and** `memory.peak` never above `memory.max` **and** at least one of: the Docker flag; a nonzero `oom_kill` in the
  cgroup's `memory.events`; a `sandbox.killed_by_sigkill` audit event.
- In the last 50 runs, `oom_kill` was nonzero in 50 of 50.

## f. Token delivery design
- The token is the first stdin line of a wrapper (`airos-token-exec`) that exports it only to the `claude` process; it is never in container config.
- Canary test with a **fake CLI** (reported): `docker inspect` showed no canary; container processes ran as a host subordinate uid, not the user's;
  `/proc/<pid>/environ` was unreadable to the user; `/proc/<pid>/cmdline` was readable but held no canary; `docker exec` was denied reading environ.
- **Never tested:** entering the rootless user namespace, ptrace, memory reads, core dumps, the real CLI.
- Working assumption: any process of the same Linux user may be able to read the token during a run, so **no other agent session may run then**.

## g. Executor image design
- Claude Code CLI 2.1.310, native musl binary, sha256 `2ceb05e0a97b09be4c14717464a2156e8d087dea9867a399dd75d3b898ca4824`, 285,302,928 bytes, on
  `alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc`; built with `--network none --pull=false`; run as uid 10001.
- Provenance reported as checked via the npm registry integrity values and a vendor signed release manifest
  (`downloads.claude.ai/claude-code-releases/2.1.310/manifest.json`; signing key fingerprint `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`,
  obtained from three channels). npm provenance attestations did not exist for that version.
- **Re-verify all hashes and the fingerprint against the vendor's official page before any reuse.**
- `DISABLE_AUTOUPDATER` is documented on the official install page. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` was never verified.

## h. Credential revocation
- `claude setup-token --help` (v2.1.319) offers only `--help` (mint-only). The human found no token revocation entry in the account settings. Public issue
  reports describe a `claude.ai/settings/claude-code` revoke page whose effectiveness was disputed.
- Decision: the live contract check (Part 2) is **deferred indefinitely** and was never run. M5 (AI Scientist v3, which also needs a subscription token in
  a full-autonomy container) is **blocked on the same condition**: a revocable credential path.
