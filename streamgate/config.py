"""All tunable numbers in one place. Override any field with a flat JSON object (--config)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, replace
from pathlib import Path

from .rules import normalize


class ConfigError(ValueError):
    pass


# must be > 0 (every other numeric field must be >= 0)
_POSITIVE = {"max_text_chars", "max_username_chars", "seen_id_window", "chars_per_s", "min_reply_s",
             "max_reply_s", "max_names", "target_utilization", "airtime_burst_s", "max_pending",
             "max_user_pending", "tick_s", "timely_s", "silence_guard_s"}
_UNIT_INTERVAL = {"target_utilization", "topic_cooldown_factor", "product_q_merge_jaccard"}
# AI-Vtuber merges messages within 1 s and keeps only the last (BRIEF §5): sends must be > 1 s apart.
_AIVTUBER_WINDOW_S = 1.0
_SEND_INTERVALS = {"min_send_interval_s", "fifo_min_send_interval_s"}


@dataclass(frozen=True)
class Config:
    # --- input hygiene -------------------------------------------------------
    max_text_chars: int = 200          # longer comments are truncated and flagged
    max_username_chars: int = 32
    seen_id_window: int = 20000        # remembered event ids for re-delivery dedupe

    # --- speech cost model (docs/FACTS.md §1, BRIEF §2) ----------------------
    chars_per_s: float = 4.5
    min_reply_s: float = 6.0
    max_reply_s: float = 18.0
    extra_chars_per_name: int = 3      # each extra addressed viewer lengthens the reply
    max_names: int = 5

    # --- channel model shared by every policy --------------------------------
    prep_est_s: float = 3.0            # expected LLM + TTS before audio starts (1-3 s + 0.5-2 s)
    pipeline_lead_s: float = 1.5       # may send this long before current audio is expected to end
    sink_backoff_s: float = 5.0        # pause after a failed send

    # --- rules ----------------------------------------------------------------
    extra_risk_words: tuple[str, ...] = ()   # operators extend the risk lexicon without code changes

    # --- Streamgate scheduler -------------------------------------------------
    target_utilization: float = 0.70   # airtime refill rate (seconds of speech per wall second)
    airtime_burst_s: float = 20.0      # airtime bucket capacity
    gift_reserve_s: float = 10.0       # gifts may borrow this much airtime (the headroom)
    gift_max_wait_s: float = 20.0      # a gift batch older than this jumps the queue
    min_send_interval_s: float = 8.0   # AI-Vtuber keeps only the last message per 1 s; brief: >= 8 s
    gather_s: float = 1.0              # let near-simultaneous asks merge before answering
    min_priority: float = 20.0
    topic_cooldown_s: float = 60.0     # same topic again soon is down-weighted, not dropped
    topic_cooldown_factor: float = 0.35
    user_cooldown_s: float = 60.0      # a viewer just answered yields to others (quota)
    max_user_pending: int = 2          # per-viewer pending cap (quota)
    dup_window_s: float = 120.0        # same viewer, same text within this window = duplicate
    max_pending: int = 300             # pool cap; overflow evicts the lowest priority (quota)
    warmth_idle_s: float = 15.0        # silence long enough to spend airtime on a welcome
    welcome_every_s: float = 120.0
    product_q_merge_jaccard: float = 0.6

    # --- FIFO baseline --------------------------------------------------------
    fifo_min_send_interval_s: float = 1.1

    # --- replay ---------------------------------------------------------------
    tick_s: float = 0.1
    drain_s: float = 60.0              # keep simulating this long after the last arrival
    timely_s: float = 60.0             # a reply later than this does not count as timely
    silence_guard_s: float = 30.0      # report WARN when the room is silent longer (product guard, not BRIEF §4)

    def __post_init__(self):
        for f in fields(self):
            v, default = getattr(self, f.name), f.default
            if isinstance(default, tuple):
                if not isinstance(v, tuple) or not all(isinstance(x, str) for x in v):
                    raise ConfigError(f"{f.name} must be a list of strings")
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ConfigError(f"{f.name} must be a number, got {v!r}")
            if isinstance(default, int) and not isinstance(v, int):
                raise ConfigError(f"{f.name} must be an integer, got {v!r}")
            positive = f.name in _POSITIVE
            if not math.isfinite(v) or v < 0 or (positive and v == 0):
                raise ConfigError(f"{f.name} must be a finite number {'> 0' if positive else '>= 0'}, got {v!r}")
            if f.name in _UNIT_INTERVAL and v > 1:
                raise ConfigError(f"{f.name} must be <= 1, got {v!r}")
            if f.name in _SEND_INTERVALS and v <= _AIVTUBER_WINDOW_S:
                raise ConfigError(f"{f.name} must be > {_AIVTUBER_WINDOW_S} s (AI-Vtuber keeps only the last "
                                  f"message per 1 s window; 1.1 or more recommended), got {v!r}")
        if self.min_reply_s > self.max_reply_s:
            raise ConfigError("min_reply_s must be <= max_reply_s")

    def with_overrides(self, **kw) -> "Config":
        return replace(self, **kw)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        if path is None:
            return cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"cannot read config {path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError("config must be a JSON object")
        unknown = sorted(set(data) - {f.name for f in fields(cls)})
        if unknown:
            raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
        if "extra_risk_words" in data:
            words = data["extra_risk_words"]
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise ConfigError("extra_risk_words must be a list of strings")
            data["extra_risk_words"] = tuple(normalize(w)[0] for w in words if w.strip())
        return cls(**data)
