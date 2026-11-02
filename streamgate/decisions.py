"""Replies, sink results, and the decision log (one final record per input event)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable

from .events import Event, Rejected
from .rules import Classification


def r3(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


@dataclass(frozen=True)
class Member:
    ev: Event
    cls: Classification


@dataclass(frozen=True)
class SinkResult:
    ok: bool
    at: float                      # when the send completed or failed
    error: str | None = None
    info: dict | None = None       # downstream timings (replay simulator only)
    maybe_delivered: bool = False  # failures only: the request may still have reached AI-Vtuber (timeout)


@dataclass
class Reply:
    reply_id: str
    policy: str
    intent: str
    slot: str | None
    members: list[Member]          # lead first
    decided_at: float
    est_s: float
    priority: float
    why: str
    max_names: int = 5
    _names: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self):
        seen: set[str] = set()
        for m in self.members:
            if m.ev.user_id not in seen:
                seen.add(m.ev.user_id)
                self._names.append(m.ev.username)

    @property
    def lead(self) -> Event:
        return self.members[0].ev

    @property
    def n_users(self) -> int:
        return len(self._names)

    @property
    def username(self) -> str:
        return self.lead.username

    @property
    def content(self) -> str:
        """Text handed to the downstream LLM/TTS (AI-Vtuber POST /send)."""
        shown = "、".join(self._names[: self.max_names])
        more = f"等{len(self._names)}位" if len(self._names) > self.max_names else ""
        if self.intent == "gift":
            return f"感谢{shown}{more}送的礼物"
        if self.intent == "welcome":
            return f"欢迎{shown}{more}来到直播间"
        if len(self._names) > 1:
            return f"{self.lead.text}（{shown}{more}都在问）"
        return self.lead.text


class DecisionLog:
    """Collects decision records. Every input event gets exactly one ``type=event`` record."""

    def __init__(self, keep: bool = True, writer: Callable[[dict], None] | None = None, tail: int = 0):
        self.records: list[dict] | None = [] if keep else None
        self.tail: deque | None = deque(maxlen=tail) if tail else None
        self.writer = writer
        self.outcomes: Counter = Counter()
        self.writer_errors = 0

    def _emit(self, rec: dict) -> None:
        if rec["type"] == "event":
            self.outcomes[(rec["outcome"], rec["reason"])] += 1
        if self.records is not None:
            self.records.append(rec)
        if self.tail is not None:
            self.tail.append(rec)
        if self.writer is not None:
            try:
                self.writer(rec)
            except Exception:   # a full disk must not stop the room
                self.writer_errors += 1

    def event(self, ev: Event, outcome: str, reason: str, at: float, cls: Classification | None = None,
              *, reply_id: str | None = None, merged_into: str | None = None, detail: str = "") -> None:
        self._emit({
            "type": "event", "seq": ev.seq, "id": ev.id, "ts": r3(ev.ts), "kind": ev.kind,
            "user_id": ev.user_id, "intent": cls.intent if cls else None,
            "outcome": outcome, "reason": reason, "at": r3(at),
            "reply_id": reply_id, "merged_into": merged_into, "detail": detail,
        })

    def rejected(self, rj: Rejected, at: float) -> None:
        self._emit({
            "type": "event", "seq": rj.seq, "id": rj.id, "ts": r3(rj.ts if rj.ts is not None else at),
            "kind": None, "user_id": None, "intent": None,
            "outcome": "dropped", "reason": rj.reason, "at": r3(at),
            "reply_id": None, "merged_into": None, "detail": rj.detail,
        })

    def reply(self, reply: Reply, result: SinkResult) -> None:
        lead = reply.lead
        rec = {
            "type": "reply", "reply_id": reply.reply_id, "policy": reply.policy,
            "intent": reply.intent, "slot": reply.slot,
            "lead_id": lead.id, "lead_seq": lead.seq, "lead_ts": r3(lead.ts),
            "member_ids": [m.ev.id for m in reply.members], "n_users": reply.n_users,
            "decided_at": r3(reply.decided_at), "sent_at": r3(result.at) if result.ok else None,
            "ok": result.ok, "error": result.error,
            "maybe_delivered": None if result.ok else result.maybe_delivered,
            "est_s": r3(reply.est_s), "priority": r3(reply.priority), "why": reply.why,
            "username": reply.username, "content": reply.content,
        }
        if result.info:
            rec.update(result.info)
        self._emit(rec)
        for i, m in enumerate(reply.members):
            if not result.ok:
                self.event(m.ev, "dropped", "sink_error", result.at, m.cls,
                           reply_id=reply.reply_id, detail=result.error or "send failed")
            elif i == 0:
                self.event(m.ev, "replied", "selected", result.at, m.cls,
                           reply_id=reply.reply_id, detail=reply.why)
            else:
                self.event(m.ev, "merged", "merged", result.at, m.cls, reply_id=reply.reply_id,
                           merged_into=lead.id, detail=f"answered together with {lead.id}")


def dumps(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False)


def digest(records: list[dict]) -> str:
    h = hashlib.sha256()
    for rec in records:
        h.update(dumps(rec).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
