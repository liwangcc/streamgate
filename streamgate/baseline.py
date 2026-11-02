"""FIFO baseline: accept everything on arrival, answer in arrival order (BRIEF §3 failure mode 1)."""

from __future__ import annotations

from collections import deque

from .channel import ChannelEstimate, estimate_reply_s
from .decisions import Member, Reply


class FifoPolicy:
    """Commits to every valid event when it arrives and voices them one by one.

    It shares the channel estimate with Streamgate (no overlapping sends, pipelined
    LLM/TTS), so the comparison isolates *what* gets answered and *when it is decided*.
    """

    name = "fifo"

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.queue: deque[Member] = deque()
        self.chan = ChannelEstimate(cfg)
        self.n_replies = 0
        self.prev_busy: dict[str, float] = {}

    def admit(self, ev, cls, now: float) -> None:
        self.queue.append(Member(ev, cls))

    def tick(self, now: float) -> Reply | None:
        if not self.queue or not self.chan.ready(now, self.cfg.fifo_min_send_interval_s):
            return None
        m = self.queue.popleft()
        est = estimate_reply_s(self.cfg, m.cls.intent, 1)
        self.n_replies += 1
        reply_id = f"r{self.n_replies:05d}"
        self.prev_busy[reply_id] = self.chan.on_send(now, est)
        return Reply(
            reply_id=reply_id, policy=self.name, intent=m.cls.intent, slot=m.cls.slot,
            members=[m], decided_at=m.ev.ts, est_s=est, priority=0.0,
            why=f"fifo: next in arrival order ({len(self.queue)} still queued)",
            max_names=self.cfg.max_names,
        )

    def on_result(self, reply: Reply, result, now: float) -> None:
        prev = self.prev_busy.pop(reply.reply_id, None)
        if not result.ok:
            self.chan.on_error(result.at, result.maybe_delivered, prev)

    def finish(self, now: float) -> None:
        while self.queue:
            m = self.queue.popleft()
            self.log.event(m.ev, "selected_unsent", "backlog", now, m.cls,
                           detail="accepted on arrival but never reached the voice channel")

    def pending(self) -> int:
        return len(self.queue)
