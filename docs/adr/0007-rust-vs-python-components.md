# ADR-0007: Rust vs Python for the sandbox runner and audit-log verifier

- Status: Proposed
- Date: 2026-11-06

## Context
Two components are security-relevant and could benefit from a memory-safe, statically linked implementation: (A) the **sandbox runner** (builds and validates container run specs, starts/stops containers) and (B) the **audit-log verifier** (recomputes the hash chain). Others (API, services, adapters, scheduler) are I/O-bound glue around Python-only SDKs (OpenAI, Anthropic, Claude Agent SDK, FastAPI, Pydantic) and belong in Python. The environment has `cargo`/`rustc` available, but no Rust code exists, and the team/deliverable scope for M1–M3 is orchestration correctness, not performance.

## Decision
**Python for everything now (M1–M5).** Re-evaluate the two candidates against explicit triggers:

| Component | Rust later? | Trigger to revisit | Why not now |
|---|---|---|---|
| (B) Audit verifier | **Yes, most likely first Rust component** — small, pure, well-specified | (1) full verification of the log exceeds 60 s on target hardware (≈ > 1–5 M entries); or (2) an auditor needs a single static binary to run offline on a separate machine; or (3) we want an *independent* implementation to guard against a spec bug shared by writer and verifier | Expected log size is thousands to low millions of entries, and SHA-256 plus JSON canonicalisation in Python is expected to be adequate at that scale (to be measured in M1 and recorded; this is an estimate, not a benchmark); a second implementation is premature before the spec is stable |
| (A) Sandbox runner | **Probably not** | Only if we stop delegating to Docker and implement our own namespace/seccomp/cgroup plumbing, or the Docker Engine API client becomes a measured bottleneck | It is a thin policy-validation layer over Docker; the risk is policy logic, not memory safety; Python lets the Pydantic policy objects, tests and golden files stay in one language |

Preparatory work now (cheap, keeps the option open): (1) freeze the chain format with RFC 8785 JCS and commit `tests/vectors/audit_chain.json`; (2) keep the verifier a pure function with no DB-engine imports beyond a row iterator; (3) expose verification as a CLI with stable exit codes (0 ok, 1 mismatch, 2 usage); (4) keep the sandbox policy as a serialisable JSON document so another implementation could consume it.

## Consequences
+ One language, one toolchain, faster M1–M3, easier review; no FFI or polyglot CI.
+ The format/vector discipline makes a later Rust verifier a drop-in, and its agreement with the Python one becomes a cross-check.
− Python offers no memory-safety guarantees for the verifier's process; mitigated by it being a pure, dependency-light function operating on rows, with no network.
− If triggers fire, a second toolchain enters CI (cargo, clippy, cross-language vector test).

## Alternatives
- **Rust for both components from the start:** better assurance and distribution, but slows M1, duplicates the contract/schema definitions, and Rust expertise/reviewer availability is unknown.
- **Go:** simpler static binaries, but offers no advantage over Rust for the verifier's assurance goal and over Python for the runner's glue role.
- **Rust for the whole control plane:** loses first-party SDKs (Anthropic/OpenAI agent SDKs) and Pydantic ergonomics.
- **Rust via PyO3 extension for hashing only:** premature optimisation; `hashlib` is already C.
