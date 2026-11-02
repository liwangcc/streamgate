"""Clocks. Only live mode reads real time; replay and tests drive a ManualClock."""

from __future__ import annotations

import time


class ManualClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def set(self, t: float) -> None:
        self.t = t

    def advance(self, dt: float) -> None:
        self.t += dt


class MonotonicClock:
    """Seconds since start, immune to wall-clock jumps."""

    def __init__(self):
        self._t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._t0
