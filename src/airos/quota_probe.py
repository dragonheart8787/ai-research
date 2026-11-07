"""Optional, read-only quota probe (A6). DISABLED by default. It is an observation for the human, NEVER a gate and NEVER a budget input:
nothing in the control plane reads its result to allow, deny or size a run. A failing probe is harmless (audited, swallowed).
No endpoint lives in this module: a probe is an injected object, and only a documented, published read-only interface may back one."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from airos.persistence.sqlite_repo import SqliteRepository


@dataclass(frozen=True)
class QuotaReading:
    remaining_usd_micro: int | None
    source: str                      # human-readable description of where the number came from


class QuotaProbe(Protocol):
    def read(self) -> QuotaReading: ...


def run_quota_probe(repo: SqliteRepository, probe: QuotaProbe | None, *, enabled: bool = False) -> QuotaReading | None:
    """Returns the reading for DISPLAY only. Disabled or failing -> None. Never raises, never changes any budget or task."""
    if not enabled or probe is None:
        return None
    try:
        reading = probe.read()
    except Exception as exc:  # noqa: BLE001 - a probe failure must never affect a run
        repo.record_system_event("quota.probe", {"ok": False, "error": type(exc).__name__})
        return None
    repo.record_system_event("quota.probe", {"ok": True, "remaining_usd_micro": reading.remaining_usd_micro, "source": reading.source[:200],
                                             "informational_only": True})
    return reading
