"""Metrics computed purely from decision records (+ optional gold labels)."""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from pathlib import Path

from .decisions import r3
from .rules import INTENTS

ANSWERABLE = {k for k, s in INTENTS.items() if s.value > 0} - {"welcome"}
REJECT_REASONS = ("malformed", "unsupported")


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dist(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": r3(percentile(xs, 50)), "p95": r3(percentile(xs, 95)),
            "max": r3(max(xs)) if xs else None}


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def load_gold(path: str | Path | None) -> dict[str, dict]:
    """id -> {"label", "should_reply", "risk"}. Bad lines are skipped (fail open)."""
    if not path or not Path(path).exists():
        return {}
    gold: dict[str, dict] = {}
    for line in Path(path).read_bytes().splitlines():
        try:
            g = json.loads(line)
            gold.setdefault(str(g["id"]), g)
        except (ValueError, KeyError, TypeError):
            continue
    return gold


def default_gold_path(replay_path: str | Path) -> Path:
    p = Path(replay_path)
    return p.with_name(p.name[: -len(".jsonl")] + ".gold.jsonl" if p.name.endswith(".jsonl") else p.name + ".gold")


def _max_overlap(intervals: list[tuple[float, float]]) -> int:
    points = sorted([(s, 1) for s, e in intervals if e > s] + [(e, -1) for s, e in intervals if e > s])
    cur = best = 0
    for _, d in points:   # ends sort before starts at equal times (-1 < 1)
        cur += d
        best = max(best, cur)
    return best


def compute(res, cfg, gold: dict[str, dict] | None = None) -> dict:
    events = [r for r in res.records if r["type"] == "event"]
    replies = [r for r in res.records if r["type"] == "reply"]
    n_input = res.lines - res.blank_lines

    per_seq = Counter(r["seq"] for r in events)
    explained = (len(per_seq) == n_input and all(c == 1 for c in per_seq.values())
                 and all(r["reason"] for r in events))

    n_rejected = sum(1 for r in events if r["reason"] in REJECT_REASONS)
    valid = n_input - n_rejected
    outcomes = Counter(r["outcome"] for r in events)
    drop_reasons = Counter(r["reason"] for r in events if r["outcome"] == "dropped")
    ok = [r for r in replies if r["ok"]]
    unsent = [r for r in events if r["outcome"] == "selected_unsent"]

    # --- latency -------------------------------------------------------------
    # first response = decision -> handed to an idle pipeline: the send itself plus any
    # time the reply then waits in the downstream audio queue. LLM/TTS/playback excluded.
    first = [r["sent_at"] - r["decided_at"] + (r.get("downstream_wait_s") or 0.0) for r in ok]
    first_censored = first + [res.horizon - r["ts"] for r in unsent]   # never sent: >= this
    viewer_wait = [r["at"] - r["ts"] for r in events if r["outcome"] in ("replied", "merged")]
    to_audio = [r["audio_start"] - r["decided_at"] for r in ok if "audio_start" in r]

    # --- channel ---------------------------------------------------------------
    plays = sorted((r["audio_start"], r["audio_end"]) for r in ok if "audio_start" in r)
    last_end = max((e for _, e in plays), default=res.t_last)
    t_end = max(res.t_last, min(last_end, res.horizon))
    span = t_end - res.t0
    busy = sum(max(0.0, min(e, t_end) - max(s, res.t0)) for s, e in plays)
    # Upper bound: a timed-out send may still have been played downstream. Count its estimated
    # length as spoken (records without the field are treated as "may have played").
    maybe_played = sum(r.get("est_s") or 0.0 for r in replies if not r["ok"] and r.get("maybe_delivered", True))
    cursor, silences = res.t0, []
    for s, e in plays:   # stretches with no confirmed audio while the stream is live
        if s > res.t_last:
            break
        silences.append(s - cursor)
        cursor = max(cursor, e)
    silences.append(res.t_last - cursor)
    max_silence = max(silences)
    sends = sorted(r["sent_at"] for r in ok)
    gaps = [b - a for a, b in zip(sends, sends[1:])]
    pipeline = [(r["sent_at"], r["audio_start"]) for r in ok if "audio_start" in r]

    committed = len(replies) + len(unsent)
    addressed = outcomes["replied"] + outcomes["merged"]

    m: dict = {
        "policy": res.policy,
        "input": {"lines": res.lines, "blank_lines": res.blank_lines, "events": n_input,
                  "valid": valid, "rejected": n_rejected},
        "window_s": {"t0": r3(res.t0), "stream_end": r3(res.t_last), "t_end": r3(t_end),
                     "horizon": r3(res.horizon)},
        "first_response_s": dist(first),
        "first_response_incl_unsent_s": dist(first_censored),
        "viewer_wait_s": dist(viewer_wait),
        "decision_to_audio_s": dist(to_audio),
        "selection": {
            "replies_sent": len(ok), "replies_failed": len(replies) - len(ok),
            "events_addressed": addressed, "selected_unsent": len(unsent),
            "selection_rate": _rate(addressed, valid),
            "reply_rate": _rate(len(ok), valid),
            "committed_rate": _rate(addressed + len(unsent) + drop_reasons.get("sink_error", 0), valid),
        },
        "outcomes": dict(sorted(outcomes.items())),
        "drop_reasons": dict(sorted(drop_reasons.items())),
        "merged": outcomes["merged"],
        "llm": {
            "scoring_calls": 0,
            "generation_calls": len(replies),
            "committed_calls": committed,
            "ratio": _rate(committed, valid),
            "ratio_executed": _rate(len(replies), valid),
        },
        "channel": {
            "utilization": _rate(busy, span) if span > 0 else None,               # confirmed
            "utilization_worst_case": _rate(busy + maybe_played, span) if span > 0 else None,
            "busy_s": r3(busy), "maybe_played_s": r3(maybe_played), "span_s": r3(span),
            "max_silence_s": r3(max_silence),
            "silence_guard_s": cfg.silence_guard_s,
            "silences_over_guard": sum(1 for g in silences if g > cfg.silence_guard_s),
            "min_send_gap_s": r3(min(gaps)) if gaps else None,
            "max_in_pipeline": _max_overlap(pipeline),
            "downstream_wait_s": dist([r.get("downstream_wait_s") or 0.0 for r in ok]),
        },
        "explained_every_event": explained,
        "internal_errors": res.internal_errors,
        "decisions_sha256": res.sha256,
    }

    # --- per-minute timeline (the backlog curve) ---------------------------------
    n_min = max(1, math.ceil(span / 60.0)) if span > 0 else 1
    arrivals, started, busy_min = Counter(), Counter(), Counter()
    for r in events:
        arrivals[int((r["ts"] - res.t0) // 60)] += 1
    for s, e in plays:
        started[int((s - res.t0) // 60)] += 1
        for k in range(int((s - res.t0) // 60), int((e - res.t0) // 60) + 1):
            lo, hi = res.t0 + 60 * k, res.t0 + 60 * (k + 1)
            busy_min[k] += max(0.0, min(e, hi) - max(s, lo))
    pending = dict(res.backlog)
    m["timeline"] = [
        {"minute": k, "arrivals": arrivals[k], "replies_started": started[k],
         "busy_frac": round(busy_min[k] / 60.0, 3),
         "pending_at_end": pending.get(round(res.t0 + 60.0 * (k + 1), 3))}
        for k in range(n_min)
    ]

    if gold:
        m["quality"] = _quality(events, replies, cfg, gold)
    return m


def _quality(events: list[dict], replies: list[dict], cfg, gold: dict[str, dict]) -> dict:
    ok = [r for r in replies if r["ok"]]

    def useful(r: dict) -> bool:
        return bool(gold.get(r["lead_id"], {}).get("should_reply"))

    def timely(r: dict) -> bool:
        return useful(r) and r["sent_at"] - r["lead_ts"] <= cfg.timely_s

    labeled = sorted((r for r in ok if r["lead_id"] in gold), key=lambda r: r["reply_id"])
    sample = random.Random(0).sample(labeled, min(100, len(labeled)))

    def risky(rs: list[dict]) -> int:
        return sum(1 for r in rs for i in r["member_ids"] if gold.get(i, {}).get("risk"))
    risk_leaks = risky(ok)
    # a timed-out send may still have been played downstream: count it separately, never as safe
    risk_leaks_unconfirmed = risky([r for r in replies if not r["ok"] and r.get("maybe_delivered", True)])

    first_by_id: dict[str, dict] = {}
    for r in events:
        if r["id"] is not None and r["reason"] not in REJECT_REASONS and r["reason"] != "duplicate":
            first_by_id.setdefault(r["id"], r)
    useful_ids = [i for i, r in first_by_id.items() if gold.get(i, {}).get("should_reply")]
    answered = [i for i in useful_ids if first_by_id[i]["outcome"] in ("replied", "merged")]
    answered_timely = [i for i in answered if first_by_id[i]["at"] - first_by_id[i]["ts"] <= cfg.timely_s]

    tp = fp = fn = tn = 0
    risk_total = risk_caught = 0
    for i, r in first_by_id.items():
        g = gold.get(i)
        if g is None or r["kind"] != "comment":
            continue
        pred = r["intent"] in ANSWERABLE
        truth = bool(g.get("should_reply"))
        tp += pred and truth
        fp += pred and not truth
        fn += truth and not pred
        tn += not pred and not truth
        if g.get("risk"):
            risk_total += 1
            risk_caught += r["intent"] == "risk"

    return {
        "labeled_replies": len(labeled),
        "effective_reply_rate": _rate(sum(map(useful, labeled)), len(labeled)),
        "effective_reply_rate_timely": _rate(sum(map(timely, labeled)), len(labeled)),
        "sample100_effective_rate": _rate(sum(map(useful, sample)), len(sample)),
        "sample100_effective_rate_timely": _rate(sum(map(timely, sample)), len(sample)),
        "sample100_n": len(sample),
        "risk_leaks": risk_leaks,
        "risk_leaks_unconfirmed": risk_leaks_unconfirmed,
        "useful_events": len(useful_ids),
        "useful_answered": len(answered),
        "useful_answered_timely_rate": _rate(len(answered_timely), len(useful_ids)),
        "rules_vs_gold": {
            "precision": _rate(tp, tp + fp), "recall": _rate(tp, tp + fn),
            "accuracy": _rate(tp + tn, tp + tn + fp + fn),
            "risk_recall": _rate(risk_caught, risk_total),
        },
    }


def summary_lines(m: dict) -> list[str]:
    fr, frc, vw = m["first_response_s"], m["first_response_incl_unsent_s"], m["viewer_wait_s"]
    sel, ch, llm = m["selection"], m["channel"], m["llm"]
    out = [
        f"policy={m['policy']}  events={m['input']['events']} valid={m['input']['valid']} "
        f"rejected={m['input']['rejected']}",
        f"first response (decision->idle pipeline) p50={fr['p50']}s p95={fr['p95']}s "
        f"| incl. never-sent p95={frc['p95']}s",
        f"viewer wait (arrival->sent)            p50={vw['p50']}s p95={vw['p95']}s",
        f"selection rate={sel['selection_rate']} replies={sel['replies_sent']} failed={sel['replies_failed']} "
        f"unsent backlog={sel['selected_unsent']}",
        f"channel utilization confirmed={ch['utilization']} worst case={ch['utilization_worst_case']} "
        f"max silence={ch['max_silence_s']}s "
        f"min send gap={ch['min_send_gap_s']}s max in pipeline={ch['max_in_pipeline']}",
        f"LLM calls committed={llm['committed_calls']} ratio={llm['ratio']} (executed ratio={llm['ratio_executed']})",
        f"drop reasons={m['drop_reasons']} merged={m['merged']}",
        f"explained every event={m['explained_every_event']} internal errors={m['internal_errors']}",
    ]
    q = m.get("quality")
    if q:
        out.append(
            f"effective reply rate={q['effective_reply_rate']} timely={q['effective_reply_rate_timely']} "
            f"| sample100={q['sample100_effective_rate']} timely={q['sample100_effective_rate_timely']} "
            f"| risk leaks={q['risk_leaks']} (+{q['risk_leaks_unconfirmed']} unconfirmed) "
            f"| useful answered<=60s={q['useful_answered_timely_rate']}"
        )
    return out
