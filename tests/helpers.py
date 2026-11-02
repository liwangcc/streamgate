from __future__ import annotations

import json

from streamgate.decisions import SinkResult
from streamgate.engine import Engine
from streamgate.events import Event


def ev(seq: int, text: str = "多少钱", ts: float = 0.0, user: str | None = None, kind: str = "comment",
       tier: str = "regular", gift: float = 0.0, eid: str | None = None, flags: tuple = ()) -> Event:
    return Event(seq=seq, id=eid or f"e{seq}", ts=ts, kind=kind, user_id=user or f"u{seq}",
                 username=f"n{seq}" if user is None else f"name-{user}", text=text, user_tier=tier,
                 gift_value=gift, flags=flags)


def line(**kw) -> bytes:
    base = {"id": "x1", "room_id": "r", "platform": "douyin", "user_id": "u1", "username": "小林",
            "text": "多少钱", "ts": 1.0, "kind": "comment", "user_tier": "regular", "gift_value": 0.0, "raw": {}}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False).encode("utf-8")


class OkSink:
    def __init__(self):
        self.sent = []

    def send(self, reply, now):
        self.sent.append(reply)
        return SinkResult(True, now)


class FailSink:
    """Times out after 2 s: the reply may still have reached AI-Vtuber."""

    def send(self, reply, now):
        return SinkResult(False, now + 2.0, "timeout", maybe_delivered=True)


class RefusedSink:
    """Connection refused: the reply certainly did not reach AI-Vtuber."""

    def send(self, reply, now):
        return SinkResult(False, now, "ConnectionRefusedError", maybe_delivered=False)


class Harness:
    """Drives an Engine with explicit times, like the replay does, but step by step."""

    def __init__(self, cfg, policy: str = "streamgate", sink=None):
        self.cfg = cfg
        self.engine = Engine(policy, cfg)
        self.sink = sink or OkSink()
        self.t = 0.0
        self.replies = []

    def admit(self, event: Event) -> None:
        self.run_until(event.ts)
        self.engine.admit(event, event.ts)

    def run_until(self, t: float) -> None:
        while self.t <= t + 1e-9:
            reply = self.engine.select(round(self.t, 6))
            if reply is not None:
                self.engine.complete(reply, self.sink.send(reply, self.t), self.t)
                self.replies.append(reply)
            self.t = round(self.t + self.cfg.tick_s, 6)

    def records(self, **match) -> list[dict]:
        return [r for r in self.engine.log.records if r["type"] == "event"
                and all(r.get(k) == v for k, v in match.items())]

    def record(self, event_id: str) -> dict:
        found = self.records(id=event_id)
        assert len(found) == 1, (event_id, found)
        return found[0]
