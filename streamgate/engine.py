"""Engine: input hygiene + fail-open wrapper around a policy. Shared by replay and live mode.

The engine never reads a clock; every call receives ``now``. That is what makes a
replay with a fixed clock reproduce the same decisions.
"""

from __future__ import annotations

from collections import OrderedDict

from .decisions import DecisionLog, Reply, SinkResult
from .events import Event, Rejected
from .rules import classify

POLICIES = ("streamgate", "fifo")


def make_policy(name: str, cfg, log):
    if name == "fifo":
        from .baseline import FifoPolicy
        return FifoPolicy(cfg, log)
    if name == "streamgate":
        from .scheduler import StreamgatePolicy
        return StreamgatePolicy(cfg, log)
    raise ValueError(f"unknown policy {name!r}; choose from {', '.join(POLICIES)}")


class Engine:
    def __init__(self, policy: str, cfg, log: DecisionLog | None = None):
        self.cfg = cfg
        self.log = log if log is not None else DecisionLog()
        self.policy = make_policy(policy, cfg, self.log)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.internal_errors = 0
        self.last_error: str | None = None

    def _internal(self, where: str, exc: Exception) -> None:
        self.internal_errors += 1
        self.last_error = f"{where}: {type(exc).__name__}: {exc}"[:300]

    def admit(self, item: Event | Rejected, now: float) -> None:
        try:
            if isinstance(item, Rejected):
                self.log.rejected(item, now)
                return
            if item.id in self._seen:
                self.log.event(item, "dropped", "duplicate", now, detail="event id already seen (re-delivery)")
                return
            self._seen[item.id] = None
            if len(self._seen) > self.cfg.seen_id_window:
                self._seen.popitem(last=False)
            cls = classify(item, self.cfg.extra_risk_words)
            self.policy.admit(item, cls, now)
        except Exception as e:   # fail open: one bad event must not stop the room
            self._internal("admit", e)
            if isinstance(item, Event):
                self.log.event(item, "dropped", "internal_error", now, detail=self.last_error or "")

    def select(self, now: float) -> Reply | None:
        try:
            return self.policy.tick(now)
        except Exception as e:
            self._internal("tick", e)
            return None

    def complete(self, reply: Reply, result: SinkResult, now: float) -> None:
        try:
            self.policy.on_result(reply, result, now)
        except Exception as e:
            self._internal("on_result", e)
        self.log.reply(reply, result)

    def finish(self, now: float) -> None:
        try:
            self.policy.finish(now)
        except Exception as e:
            self._internal("finish", e)

    def pending(self) -> int:
        try:
            return self.policy.pending()
        except Exception:
            return -1
