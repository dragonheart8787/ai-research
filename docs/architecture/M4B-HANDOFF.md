# M4b hand-off: what the human runs on the Ubuntu VM

M4a was built in a cloud workspace with **no Docker daemon**. Everything that needs one is written but **unverified**. This file is the complete
procedure to verify it. Nothing here needs a provider login or token: **do not log in to Claude or ChatGPT/Codex for these steps**, and do not put
`CLAUDE_CODE_OAUTH_TOKEN` or any Codex login on the VM for them. Adapters stay disabled.

## 0. Snapshot first
Take a hypervisor snapshot of the VM named `pre-m4b` and keep it until you have read the results. The steps below install a Docker daemon (rootless)
and create a network and an image; restoring the snapshot removes all of it. Do **not** run any research, GPU, training or AI Scientist workload.

## 1. Rootless Docker (Ubuntu 24.04) - commands written from memory, check them against Docker's current rootless docs
```bash
sudo apt-get update
sudo apt-get install -y uidmap dbus-user-session curl ca-certificates
# Docker Engine + rootless extras from Docker's apt repository (see docs.docker.com/engine/install/ubuntu), then:
sudo apt-get install -y docker-ce-cli docker-ce-rootless-extras
sudo systemctl disable --now docker.service docker.socket   # the ROOT daemon must not be the one the tests talk to
dockerd-rootless-setuptool.sh install
loginctl enable-linger "$USER"
systemctl --user enable --now docker
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock       # put it in your shell profile
```
Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor; if `dockerd-rootless` fails with a permission error on `newuidmap`/`unshare`, apply
the AppArmor profile for rootlesskit that Docker's docs describe (or, as a last resort for a throw-away VM only,
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`) and **write down which you used**.

Check it really is rootless and the root socket is not in play:
```bash
docker info --format '{{.SecurityOptions}}'     # expected: contains "name=rootless"
docker context ls                                # expected: DOCKER_HOST is the user socket, not /var/run/docker.sock
ls -l /var/run/docker.sock                       # expected: No such file (root daemon disabled)
```
**Proves:** a rootless daemon exists. **Does not prove:** anything about our specs.

## 2. Clone, branch, environment from the lockfile
```bash
git clone https://github.com/dragonheart8787/ai-research.git && cd ai-research
git checkout claude/m4-sandbox
curl -LsSf https://astral.sh/uv/install.sh | sh        # or: pipx install uv
uv venv --python 3.11 .venv                             # the lockfile is compiled for Python 3.11 (Ubuntu 24.04 ships 3.12)
uv pip sync --python .venv/bin/python requirements/dev.lock
.venv/bin/python scripts/check_lock.py requirements/dev.lock    # expected: "lock check ok: installed packages match requirements/dev.lock"
```

## 3. Baseline (no container involved)
```bash
.venv/bin/ruff check src tests && .venv/bin/python -m mypy --strict src
.venv/bin/python -m pytest -q -rs
```
Expected: ruff and mypy clean; `708 passed, 6 skipped`. The 6 skips are `tests/test_container_real.py`, each with the reason
`AIROS_TEST_DOCKER=1 is not set`. If the numbers differ, stop and report.
**Proves:** the checkout and environment reproduce the cloud result. **Does not prove:** anything about containers.

## 4. Real-container tests (the M4b core)
```bash
docker build -t airos-local/test:dev docker/test-image        # busybox + USER 10001; no credentials
AIROS_TEST_DOCKER=1 .venv/bin/python -m pytest tests/test_container_real.py -v -rs
```
Expected if everything we wrote is right: `6 passed`, 0 skipped. Each test and what a PASS means (and does not):

| Test | A pass proves | It does not prove |
|---|---|---|
| `test_runs_as_the_unprivileged_uid` | the `--user 10001:10001` we emit takes effect | the image has no setuid binaries |
| `test_root_filesystem_is_read_only_...` | `--read-only` holds; `/work` and `/outputs` are writable; `/inputs` is read-only | that no other writable path exists inside a real agent image |
| `test_all_capabilities_are_dropped_...` | `CapEff` is zero and `NoNewPrivs` is 1 | seccomp strength; kernel-level escapes |
| `test_the_docker_socket_and_host_paths_are_not_visible` | the socket and `/home` are not mounted | that a bug in a future mount list cannot expose them (the validator tests cover the list) |
| `test_the_host_environment_does_not_leak_...` | only the named token is forwarded and it is not in any process listing | that a real agent cannot read the token it was given |
| `test_the_internal_network_has_no_route_out` | an `--internal` network gives no route to the internet | that the allow-list **proxy** works: it is NOT built or tested yet (see 6) |

**Known risk to expect:** with a rootless daemon, host directories created by the control plane (mode 0700, owned by you) are mapped to a different
uid inside the container, so `touch /work/a` may fail with `Permission denied` even though the flags are right. If that is the only failure, **record
it and report it; do not loosen any flag to make it pass.** The fix is a deliberate M4b code decision (directory ownership/mapping), not a test edit.

## 5. Cleanup
```bash
docker network rm airos-net-planner 2>/dev/null; docker image rm airos-local/test:dev
systemctl --user stop docker        # optional; or restore the pre-m4b snapshot
```

## 6. The live contract check (a STUB today)
```bash
AIROS_LIVE=1 .venv/bin/python -m airos.sandbox.live_check; echo "exit=$?"
```
Expected output **today** (M4a deliberately ships it failing closed):
```
FAIL (closed): the live contract check is not implemented in M4a (it is M4b work on the VM); nothing was verified
exit=3
```
Without `AIROS_LIVE=1` it prints a refusal and exits 2. **Proves:** only that the stub cannot report success. It reads no token and makes no call.
Do not treat exit 3 as a problem and do not try to "make it pass".

## 7. Everything still unverified after you finish this file (M4b work, in order)
1. Implement the live contract check; it will need your own subscription login **on the VM only**, decided by you at that point. It must record: the real `claude -p` and `codex exec` flags and output shapes; the field that reports the credential source (our `apiKeySource` is an assumption); the wording of a usage-limit message; the hosts each CLI actually contacts (our `config/egress.toml` is an assumption); that `codex login status` reports a ChatGPT login and that no API key was auto-created.
2. Build the per-role allow-list egress proxy and test that a non-allow-listed host is blocked and an allow-listed one is reachable.
3. Build the executor image (Agent SDK) and the container-backed `AgentLoop`; pin image digests.
4. Decide the host-directory ownership/mapping under rootless Docker (see the known risk in 4).
5. Container-backed check runner for `reproduce` checks (until then they are `unverifiable` = rejected).
6. gVisor/other hardening decisions (ADR-0006 item 5).
