"""Hash-chain primitives for the audit log (pure; no database imports).

entry_hash = sha256( prev_hash_hex + "\\n" + canonical_json(material) ), genesis prev_hash = 64 x "0".
`material` = every event field except prev_hash / entry_hash. See ADR-0003.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from airos.canonical import canonical_json, sha256_hex

GENESIS_HASH = "0" * 64


def entry_material(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "event_id": row["event_id"],
        "ts_ms": row["ts_ms"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "event_type": row["event_type"],
        "actor_id": row["actor_id"],
        "actor_kind": row["actor_kind"],
        "from_state": row["from_state"],
        "to_state": row["to_state"],
        "payload": json.loads(row["payload_json"]),
    }


def compute_entry_hash(prev_hash: str, material: Mapping[str, Any]) -> str:
    return sha256_hex(prev_hash + "\n" + canonical_json(dict(material)))


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    checked: int
    broken_seq: int | None  # exact seq (index) of the first entry that fails verification
    reason: str | None
    head_seq: int
    head_hash: str


def verify_rows(rows: Iterable[Mapping[str, Any]]) -> ChainVerification:
    """Verify rows in ascending seq order. Pure function (a second-language verifier can mirror it)."""
    prev_hash, prev_seq, checked = GENESIS_HASH, 0, 0
    for row in rows:
        seq = row["seq"]
        if seq != prev_seq + 1:
            return ChainVerification(False, checked, prev_seq + 1, f"gap: expected seq {prev_seq + 1}, found {seq}", prev_seq, prev_hash)
        if row["prev_hash"] != prev_hash:
            return ChainVerification(False, checked, seq, "prev_hash does not match previous entry_hash", prev_seq, prev_hash)
        try:
            expect = compute_entry_hash(prev_hash, entry_material(row))
        except (ValueError, TypeError) as exc:
            return ChainVerification(False, checked, seq, f"unparseable entry: {exc}", prev_seq, prev_hash)
        if row["entry_hash"] != expect:
            return ChainVerification(False, checked, seq, "entry_hash mismatch (entry content was altered)", prev_seq, prev_hash)
        prev_hash, prev_seq, checked = row["entry_hash"], seq, checked + 1
    return ChainVerification(True, checked, None, None, prev_seq, prev_hash)
