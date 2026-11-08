# ADR-0003: Append-only, hash-chained audit log

- Status: Proposed
- Date: 2026-11-06

## Context
The system's value is that its history can be trusted: who approved what, which agent produced which artifact, what was spent. Operators and (later) auditors must be able to detect after-the-fact edits, deletions, reordering and truncation. Agents are untrusted code producers; the DB file is writable by anyone with host access.

## Decision
1. All state changes write a row to `task_events` **in the same transaction** as the change. UPDATE/DELETE on `task_events` are blocked by triggers.
2. Each row has `seq` (gapless), `prev_hash`, `entry_hash = sha256(prev_hash_hex ‖ "\n" ‖ JCS(entry_without_hashes))`, genesis `prev_hash = 64×"0"`. Canonicalisation: RFC 8785 JCS, integers only.
3. Payloads are secret-redacted **before** hashing.
4. Verification: `airos audit verify` and `GET /v1/audit/verify`; run at startup on the tail (last 1 000, or full if ≤ 100 000 entries).
5. Head anchoring: every 1 000 events or 24 h, append `(seq, entry_hash, ts)` to `anchors/audit-anchors.log` on a separate volume; optional publication to GitHub is a human decision (Open question Q4).
6. Language-neutral test vectors in `tests/vectors/audit_chain.json` define the format so independent verifiers can exist (ADR-0007).

## Consequences
+ Detects: modified payload, deleted/reordered/truncated rows (if anchor is newer than truncation point), forged middle entries.
+ Atomicity with state change means "no unlogged transitions" is checkable (TL-01).
− **Limits:** an attacker able to rewrite the whole DB *and* all anchors can forge a consistent chain; tamper *evidence*, not prevention. Documented as risk #4.
− One global chain serialises writes (acceptable under ADR-0001's single writer); sharding would need per-shard chains plus a Merkle root.
− Redaction is irreversible: a mistakenly redacted field cannot be recovered from the log.
− Tiny per-write cost (one SHA-256, one prior-row read — kept in memory by the writer).

## Alternatives
- **Plain append-only table (triggers only):** prevents casual edits but not detection after file-level tampering.
- **Merkle tree / transparency log (e.g., Trillian, sigstore Rekor):** stronger and supports inclusion proofs, but heavy infra and external dependency; revisit if external auditing is needed.
- **Per-entry HMAC with a secret key:** detects edits by outsiders without the key but not by the key holder, and creates key-management burden; could be layered later for signing anchors.
- **External log service (SaaS):** introduces an outbound dependency and publishes data off-host.
- **Event sourcing as the primary store:** over-reaching; we keep current-state tables plus an audit log.
