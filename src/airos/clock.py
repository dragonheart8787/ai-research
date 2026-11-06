"""Injectable clock. All timestamps are UTC epoch milliseconds."""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FakeClock:
    """Deterministic clock for tests; never sleeps."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> int:
        if ms < 0:
            raise ValueError("time only moves forward")
        self._now += ms
        return self._now
