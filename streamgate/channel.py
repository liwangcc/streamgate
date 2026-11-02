"""Speech cost model and the policy-side view of the single voice channel."""

from __future__ import annotations

import math

from .rules import INTENTS


def estimate_reply_s(cfg, intent: str, n_users: int) -> float:
    """Expected spoken length of a reply: typical chars for the intent, plus named viewers."""
    spec = INTENTS.get(intent, INTENTS["chitchat"])
    names = max(1, min(n_users, cfg.max_names))
    chars = spec.reply_chars + cfg.extra_chars_per_name * (names - 1)
    return min(cfg.max_reply_s, max(cfg.min_reply_s, chars / cfg.chars_per_s))


class ChannelEstimate:
    """What a policy believes about the voice channel.

    Streamgate gets no playback callback from AI-Vtuber, so it estimates: a reply sent
    at t is expected to start at t + prep_est and last est_s. The replay's downstream
    simulator holds the real timings and measures how wrong this estimate is.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.busy_until = -math.inf
        self.last_send = -math.inf
        self.backoff_until = -math.inf

    def ready(self, now: float, min_interval_s: float) -> bool:
        return (
            now >= self.last_send + min_interval_s
            and now >= self.busy_until - self.cfg.pipeline_lead_s
            and now >= self.backoff_until
        )

    def on_send(self, now: float, est_s: float) -> float:
        """Returns the previous busy_until, so a send that certainly failed can be undone."""
        prev = self.busy_until
        self.last_send = now
        self.busy_until = max(now + self.cfg.prep_est_s, self.busy_until) + est_s
        return prev

    def on_error(self, now: float, maybe_delivered: bool = True, prev_busy_until: float | None = None) -> None:
        # A timed-out send may still play downstream, so its airtime stays reserved (no overlap risk).
        # A refused connection or an HTTP error reply certainly did not play: free the channel again.
        self.backoff_until = now + self.cfg.sink_backoff_s
        if not maybe_delivered and prev_busy_until is not None:
            self.busy_until = prev_busy_until

    def idle_for(self, now: float) -> float:
        return now - self.busy_until
