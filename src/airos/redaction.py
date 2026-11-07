"""Secret redaction. Applied to every audit write and every error write.

Two mechanisms: (1) exact values registered at load time (min length 8), (2) well-known patterns.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, flags)
    for p, flags in (
        (r"sk-ant-[A-Za-z0-9_\-]{16,}", 0),
        (r"sk-[A-Za-z0-9_\-]{20,}", 0),
        (r"gh[pousr]_[A-Za-z0-9]{20,}", 0),
        (r"github_pat_[A-Za-z0-9_]{20,}", 0),
        (r"AKIA[0-9A-Z]{16}", 0),
        (r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", 0),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        (r"bearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    )
)


class Redactor:
    MIN_SECRET_LEN = 8

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for s in secrets:
            self.register_secret(s)

    def __repr__(self) -> str:
        return f"Redactor(<{len(self._secrets)} registered secret(s)>)"

    @classmethod
    def from_environ(cls, names: Iterable[str]) -> Redactor:
        return cls(os.environ[n] for n in names if os.environ.get(n))

    def register_secret(self, value: str) -> None:
        if len(value) >= self.MIN_SECRET_LEN and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)  # longest first

    def redact_text(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, REDACTED)
        for pat in _PATTERNS:
            text = pat.sub(REDACTED, text)
        return text

    def contains_secret(self, text: str) -> bool:
        return self.redact_text(text) != text

    def redact_json(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, dict):
            return {self.redact_text(str(k)): self.redact_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_json(v) for v in obj]
        return obj
