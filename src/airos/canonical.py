"""Canonical JSON (RFC 8785-compatible subset: strings, integers, bools, null, arrays, objects).

Floats are rejected on purpose: money is integer micro-USD, durations are integer ms.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _check(obj: Any) -> None:
    if isinstance(obj, float):
        raise TypeError("floats are not allowed in canonical JSON")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError("object keys must be strings")
            _check(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _check(v)
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise TypeError(f"unsupported type in canonical JSON: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
