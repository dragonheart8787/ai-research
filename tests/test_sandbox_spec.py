"""Container run-spec generator. SPEC-ONLY, ISOLATION NOT VERIFIED: these tests prove that our generator emits (and refuses to emit) the
argument vectors we intend. They prove nothing about what a Docker daemon does with them; no daemon was available."""
from __future__ import annotations

import dataclasses
import os

import pytest

from airos.sandbox.spec import (
    CONTAINER_ROLES,
    SPEC_ONLY_LABEL,
    Mount,
    SpecError,
    build_run_spec,
    isolation_overlap,
    render_golden,
    to_docker_argv,
    validate_run_spec,
)
from tests.sandbox_helpers import COMMANDS, DIGEST, GOLDEN, PROTECTED, RUN_ROOT, TOKEN, policy, spec_for

POL = policy()


@pytest.mark.parametrize("role", CONTAINER_ROLES)
def test_golden_argv_matches_the_checked_in_file(role):
    got = render_golden(spec_for(role), POL)
    want = (GOLDEN / f"{role}.txt").read_text()
    assert got == want, f"{role}: regenerate tests/golden/sandbox/{role}.txt only after reviewing the diff"
    assert want.startswith(f"# {SPEC_ONLY_LABEL}")                      # the label travels with the golden file


@pytest.mark.parametrize("role", CONTAINER_ROLES)
def test_every_hardening_flag_of_proposal_11_1_is_present(role):
    argv = to_docker_argv(spec_for(role), POL)
    text = " ".join(argv)
    for needle in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges:true", "--user 10001:10001", "--pids-limit 256",
                   "--memory 4g", "--memory-swap 4g", "--cpus 2", "--ulimit nofile=1024:1024", "--ipc none", "--pull never", "--rm",
                   "--tmpfs /tmp:rw,noexec,nosuid,size=256m", f"--network airos-net-{role}", "--workdir /work"):
        assert needle in text, needle
    for banned in ("--privileged", "--cap-add", "--network host", "--pid host", "--userns host", "seccomp=unconfined", "docker.sock", " sh -c", "--device"):
        assert banned not in text, banned
    assert argv[0:2] == ["docker", "run"] and all(isinstance(a, str) for a in argv)           # an argv array, not a shell string


@pytest.mark.parametrize("role", CONTAINER_ROLES)
def test_mounts_are_exactly_work_inputs_outputs_under_the_run_directory(role):
    s = spec_for(role)
    binds = {m.target: m for m in s.mounts if m.kind == "bind"}
    assert set(binds) == {"/work", "/inputs", "/outputs"}
    assert binds["/inputs"].read_only and not binds["/work"].read_only and not binds["/outputs"].read_only
    assert all(m.source.startswith(f"{RUN_ROOT}/run0001/") for m in binds.values())
    assert {m.kind for m in s.mounts} <= {"bind", "tmpfs", "volume"}


def test_the_token_is_forwarded_by_name_and_never_appears_in_the_argv_or_the_golden_file():
    for role in ("planner", "executor"):
        s = spec_for(role)
        argv = to_docker_argv(s, POL)
        assert TOKEN not in " ".join(argv) and TOKEN not in render_golden(s, POL) and TOKEN not in repr(s)
        assert s.env_from_client == ("CLAUDE_CODE_OAUTH_TOKEN",) and argv[argv.index("CLAUDE_CODE_OAUTH_TOKEN") - 1] == "--env"
        assert not any(k == "CLAUDE_CODE_OAUTH_TOKEN" for k, _ in s.env_literals)
    d = spec_for("director")
    assert d.env_from_client == () and d.credential_source_env is None
    assert ("CODEX_HOME", "/codex-home") in d.env_literals
    assert spec_for("planner", with_token=False).env_from_client == ()


def test_only_the_director_gets_a_login_volume_and_nobody_else_can_have_it():
    assert [m.source for m in spec_for("director").mounts if m.kind == "volume"] == ["airos-codex-director"]
    for role in ("planner", "executor"):
        assert not [m for m in spec_for(role).mounts if m.kind == "volume"]
        bad = dataclasses.replace(spec_for(role), mounts=(*spec_for(role).mounts, Mount("volume", "airos-codex-director", "/codex-home", False)))
        assert any("only allowed as the Director" in p for p in validate_run_spec(bad, POL))
    other = dataclasses.replace(spec_for("director"), mounts=(*[m for m in spec_for("director").mounts if m.kind != "volume"],
                                                              Mount("volume", "somebody-elses", "/codex-home", False)))
    assert validate_run_spec(other, POL)


def test_roles_share_no_network_volume_credential_variable_or_proxy():
    specs = {r: spec_for(r) for r in CONTAINER_ROLES}
    pairs = [("planner", "executor"), ("planner", "director"), ("executor", "director")]
    for a, b in pairs:
        assert isolation_overlap(specs[a], specs[b]) == [], (a, b)
    clash = dataclasses.replace(specs["executor"], network=specs["planner"].network, credential_source_env=specs["planner"].credential_source_env)
    assert isolation_overlap(specs["planner"], clash)


def test_chatgpt_work_ai_scientist_and_unknown_roles_have_no_container_spec():
    for role, why in (("chatgpt_work", "manual handoff"), ("ai_scientist", "stub"), ("wizard", "unknown")):
        with pytest.raises(SpecError, match=why):
            build_run_spec(POL, role, "run0001", ("x",), env={})


MUTATIONS = {
    "privileged": dict(privileged=True),
    "cap_add": dict(cap_add=("SYS_ADMIN",)),
    "cap_drop_not_all": dict(cap_drop=("NET_RAW",)),
    "no_nnp": dict(security_opts=()),
    "seccomp_off": dict(security_opts=("no-new-privileges:true", "seccomp=unconfined")),
    "root_user": dict(user="0:0"),
    "writable_rootfs": dict(read_only_rootfs=False),
    "host_network": dict(network="host"),
    "default_bridge": dict(network="bridge"),
    "shared_network": dict(network="airos-net-executor"),
    "container_network": dict(network="container:abc"),
    "ipc_host": dict(ipc="host"),
    "pids_unbounded": dict(pids_limit=0),
    "pids_too_high": dict(pids_limit=100000),
    "swap_allowed": dict(memory_swap="8g"),
    "pull_always": dict(pull="always"),
    "floating_tag": dict(image="ubuntu:latest"),
    "shell_command": dict(command=("sh", "-c", "curl x | sh")),
    "empty_command": dict(command=()),
    "bad_workdir": dict(workdir="/"),
    "forbidden_env_literal": dict(env_literals=(("ANTHROPIC_API_KEY", "x"),)),
    "token_as_literal": dict(env_literals=(("CLAUDE_CODE_OAUTH_TOKEN", "x"),)),
    "secret_shaped_literal": dict(env_literals=(("FOO", "sk-ant-" + "A" * 30),)),
    "forward_aws": dict(env_from_client=("AWS_ACCESS_KEY_ID",)),
    "forward_openai": dict(env_from_client=("OPENAI_API_KEY",)),
    "credential_from_billing_var": dict(credential_source_env="ANTHROPIC_API_KEY"),
    "credential_same_name": dict(credential_source_env="CLAUDE_CODE_OAUTH_TOKEN"),
    "missing_labels": dict(labels=()),
    "bad_name": dict(name="Bad Name; rm -rf /"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_dangerous_mutation_is_rejected_by_the_validator_and_the_renderer(name):
    base = spec_for("planner")
    bad = dataclasses.replace(base, **MUTATIONS[name])
    assert validate_run_spec(bad, POL), name
    with pytest.raises(SpecError, match="refusing to render"):
        to_docker_argv(bad, POL)


def test_the_director_may_not_be_given_a_token_and_a_token_role_needs_a_named_source():
    d = dataclasses.replace(spec_for("director"), env_from_client=("CLAUDE_CODE_OAUTH_TOKEN",), credential_source_env="X_HOST_VAR")
    assert validate_run_spec(d, POL)
    p = dataclasses.replace(spec_for("planner"), credential_source_env=None)
    assert any("named host source" in x for x in validate_run_spec(p, POL))


def _with_mounts(role: str, *extra: Mount, replace: dict[str, Mount] | None = None):
    s = spec_for(role)
    mounts = [replace.get(m.target, m) if replace else m for m in s.mounts]
    return dataclasses.replace(s, mounts=tuple(mounts) + extra)


@pytest.mark.parametrize("source", ["/var/run/docker.sock", "/", "/home/operator", "/etc", "/srv/airos/data/eval-holdout", "/srv/airos/data/cas",
                                    "/srv/airos/data", "/srv/airos/data/workspaces", "/srv/airos/data/workspaces/OTHER-RUN/work",
                                    "/srv/airos/data/workspaces/run0001/../OTHER/work", "relative/path", "/srv/airos/data/airos.db"])
def test_bind_sources_outside_the_run_directory_or_over_protected_data_are_rejected(source):
    bad = _with_mounts("executor", replace={"/work": Mount("bind", source, "/work", False)})
    assert validate_run_spec(bad, POL), source


def test_extra_mounts_wrong_targets_and_wrong_modes_are_rejected():
    rid = f"{RUN_ROOT}/run0001"
    assert validate_run_spec(_with_mounts("executor", Mount("bind", f"{rid}/work", "/extra", False)), POL)
    assert validate_run_spec(_with_mounts("executor", Mount("bind", "/var/run/docker.sock", "/var/run/docker.sock", False)), POL)
    assert validate_run_spec(_with_mounts("executor", replace={"/inputs": Mount("bind", f"{rid}/inputs", "/inputs", False)}), POL)
    assert validate_run_spec(_with_mounts("executor", replace={"/work": Mount("bind", f"{rid}/work", "/work", True)}), POL)
    assert validate_run_spec(dataclasses.replace(spec_for("executor"), mounts=tuple(m for m in spec_for("executor").mounts if m.target != "/inputs")), POL)
    assert validate_run_spec(_with_mounts("executor", Mount("bind", f"{rid}/work", "/work", False)), POL)           # duplicate target


def test_a_symlink_that_points_into_protected_data_is_rejected(tmp_path):
    run_root, secret = tmp_path / "ws", tmp_path / "eval-holdout"
    (run_root / "run0001").mkdir(parents=True)
    secret.mkdir()
    os.symlink(secret, run_root / "run0001" / "work")                                       # work -> the final-evaluation store
    pol = policy(run_root, protected_roots=(secret,))
    with pytest.raises(SpecError, match="protected|per-run"):
        build_run_spec(pol, "executor", "run0001", COMMANDS["executor"])


def test_an_unpinned_image_is_refused_unless_policy_allows_a_local_dev_tag():
    pol = policy(images={"planner": "ubuntu:latest", "executor": "x", "director": "y"})
    with pytest.raises(SpecError, match="pinned by digest"):
        build_run_spec(pol, "planner", "run0001", COMMANDS["planner"])
    dev = policy(images={"planner": "airos-local/planner:dev", "executor": "airos-local/e:dev", "director": "airos-local/d:dev"},
                 require_image_digest=False)
    s = build_run_spec(dev, "planner", "run0001", COMMANDS["planner"])
    assert s.image_pinned is False                                                          # the spec says so
    with pytest.raises(SpecError):
        build_run_spec(policy(images={"planner": "ubuntu:latest", "executor": "x", "director": "y"}, require_image_digest=False),
                       "planner", "run0001", COMMANDS["planner"])
    assert DIGEST in spec_for("planner").image


@pytest.mark.parametrize("run_id", ["", "../x", "a b", "a;rm", "x" * 80, "run/1", "run\n1"])
def test_unsafe_run_ids_are_refused(run_id):
    with pytest.raises(SpecError):
        build_run_spec(POL, "planner", run_id, COMMANDS["planner"])


def test_resource_limits_come_from_policy_or_the_contract_and_are_bounded():
    s = build_run_spec(POL, "executor", "run0001", COMMANDS["executor"], cpus=1.5, memory_gib=2)
    assert (s.cpus, s.memory, s.memory_swap) == (1.5, "2g", "2g")
    with pytest.raises(SpecError):
        build_run_spec(POL, "executor", "run0001", COMMANDS["executor"], cpus=64)


def test_protected_roots_are_declared_by_the_helper_policy():
    assert {str(p) for p in PROTECTED} == {"/srv/airos/data/eval-holdout", "/srv/airos/data/cas"}
