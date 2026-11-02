"""Input validation. Anything we cannot trust becomes a ``Rejected`` record, never an exception."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

KINDS = ("comment", "gift", "follow", "entrance")
TIERS = ("regular", "new", "fan", "gift")

# C0/C1 controls, zero-width and bidi-override characters
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Event:
    seq: int           # arrival order (line number in a replay, counter when live)
    id: str
    ts: float          # arrival time on the scheduler's clock
    kind: str
    user_id: str
    username: str
    text: str
    user_tier: str = "regular"
    gift_value: float = 0.0
    platform: str = "douyin"
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejected:
    seq: int
    reason: str        # "malformed" | "unsupported"
    detail: str
    id: str | None = None
    ts: float | None = None


def _clean(s: str, limit: int) -> tuple[str, bool]:
    s = _WS.sub(" ", _CTRL.sub("", s)).strip()
    return (s[:limit], True) if len(s) > limit else (s, False)


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _ident(v) -> str | None:
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return None
    s = _clean(str(v), 64)[0]
    return s or None


def parse_line(line: str | bytes, seq: int, cfg, arrival_ts: float | None = None) -> Event | Rejected:
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8-sig")
        obj = json.loads(line)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        return Rejected(seq, "malformed", f"invalid JSON ({type(e).__name__})")
    return parse_obj(obj, seq, cfg, arrival_ts)


def parse_obj(obj, seq: int, cfg, arrival_ts: float | None = None) -> Event | Rejected:
    """Validate one event. In live mode ``arrival_ts`` replaces the client-supplied ``ts``."""
    if not isinstance(obj, dict):
        return Rejected(seq, "malformed", f"expected a JSON object, got {type(obj).__name__}")
    flags: list[str] = []

    eid = _ident(obj.get("id"))
    if eid is None:
        eid = f"_line{seq}"
        flags.append("no_id")

    if arrival_ts is not None:
        ts = arrival_ts
    else:
        ts = _num(obj.get("ts"))
        if ts is None or ts < 0:
            return Rejected(seq, "malformed", "ts missing, not a finite number, or negative", eid)

    kind = obj.get("kind", "comment")
    if kind not in KINDS:
        return Rejected(seq, "unsupported", f"kind {str(kind)[:20]!r} is not handled", eid, ts)

    text = obj.get("text")
    if text is None and kind != "comment":
        text = ""
    if not isinstance(text, str):
        return Rejected(seq, "malformed", f"text is {type(text).__name__}, expected string", eid, ts)
    text, truncated = _clean(text, cfg.max_text_chars)
    if truncated:
        flags.append("truncated")
    if kind == "comment" and not text:
        return Rejected(seq, "malformed", "empty text", eid, ts)

    user_id = _ident(obj.get("user_id"))
    if user_id is None:
        user_id = f"_anon:{eid}"   # anonymous viewers never dedupe against each other
        flags.append("no_user")
    username = obj.get("username")
    username = _clean(username, cfg.max_username_chars)[0] if isinstance(username, str) else ""
    username = username or user_id

    tier = obj.get("user_tier", "regular")
    if tier not in TIERS:
        tier = "regular"
        flags.append("bad_tier")

    gift_value = 0.0
    if "gift_value" in obj:
        gv = _num(obj.get("gift_value"))
        if gv is None or gv < 0:
            flags.append("bad_gift_value")
        else:
            gift_value = gv

    platform = obj.get("platform")
    platform = _clean(platform, 16)[0] if isinstance(platform, str) else ""

    return Event(
        seq=seq, id=eid, ts=ts, kind=kind, user_id=user_id, username=username, text=text,
        user_tier=tier, gift_value=gift_value, platform=platform or "douyin", flags=tuple(flags),
    )
