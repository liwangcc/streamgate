"""A/B comparison of FIFO vs Streamgate on one replay, with the BRIEF §4 acceptance table.

Row ``pass`` values: True/False (checked), None (informational), "PENDING" (needs human input).
"""

from __future__ import annotations

from .metrics import compute
from .replay import run_replay

PENDING = "PENDING"
WARN = "WARN"          # product guard exceeded: not a BRIEF §4 failure, but never silently "fine"
# BRIEF §4 is defined on a ~30-minute, ~100 events/min replay; below this scale rate rows mean little.
ACCEPTANCE_MIN_EVENTS = 1000
ACCEPTANCE_MIN_SPAN_S = 20 * 60


def run_both(lines, cfg, sim, gold) -> dict:
    out = {}
    for policy in ("fifo", "streamgate"):
        first = run_replay(lines, policy, cfg, sim)
        second = run_replay(lines, policy, cfg, sim)          # determinism: fresh engine, same input
        m = compute(first, cfg, gold)
        m["deterministic"] = first.sha256 == second.sha256
        out[policy] = (first, m)
    return out


def _q(m: dict, key: str):
    return (m.get("quality") or {}).get(key)


def scale_note(m: dict) -> str | None:
    """Explains why a small replay is a smoke test, or None at acceptance scale."""
    n = m["input"]["valid"]
    span = (m["window_s"]["stream_end"] or 0) - (m["window_s"]["t0"] or 0)
    if n >= ACCEPTANCE_MIN_EVENTS and span >= ACCEPTANCE_MIN_SPAN_S:
        return None
    return (f"SMOKE TEST, not acceptance: {n} valid events over {span / 60:.1f} min. BRIEF §4 is defined on a "
            f"~30 min, ~100 events/min replay; rate rows (effective rate, LLM ratio) are not meaningful here.")


def _risk_cell(m: dict):
    q = m.get("quality")
    if not q:
        return "unverified"
    unconfirmed = q.get("risk_leaks_unconfirmed", 0)
    return q["risk_leaks"] if not unconfirmed else f"{q['risk_leaks']} (+{unconfirmed} unconfirmed)"


def _risk_ok(m: dict):
    """Without gold labels "0 risk voiced" cannot be shown, so it is PENDING, never informational."""
    q = m.get("quality")
    if not q:
        return PENDING
    return q["risk_leaks"] == 0 and q.get("risk_leaks_unconfirmed", 0) == 0


def _in_range(u) -> bool:
    return u is not None and 0.60 <= u <= 0.80


def acceptance(fifo: dict, sg: dict, human: dict | None = None) -> list[dict]:
    """``human`` is the result of annotate.score() for a blind sheet covering both policies.

    ``required`` rows are BRIEF §4 criteria: verdict() treats a required row without a result as
    PENDING, so "could not be checked" never counts as "passed".
    """
    def row(metric, target, f, s, ok, required=False):
        return {"metric": metric, "target": target, "fifo": f, "streamgate": s, "pass": ok, "required": required}

    fr_f, fr_s = fifo["first_response_s"]["p95"], sg["first_response_s"]["p95"]
    util_s, util_worst = sg["channel"]["utilization"], sg["channel"].get("utilization_worst_case")
    llm_s = sg["llm"]["ratio"]
    risk_metric = ("risk messages voiced (gold; failed sends that may have played counted as unconfirmed)"
                   if sg.get("quality") else "risk messages voiced (cannot verify: gold labels missing)")

    hp = (human or {}).get("per_policy", {})
    h_f, h_s = hp.get("fifo"), hp.get("streamgate")
    if h_f and h_s and human.get("complete"):
        rf, rs = h_f["effective_reply_rate"], h_s["effective_reply_rate"]
        human_rows = [
            row(f"effective reply rate, human labels (n={h_s['labeled']} / {h_f['labeled']})",
                ">= 0.70 and >= FIFO + 0.30", rf, rs,
                rf is not None and rs is not None and rs >= 0.70 and rs - rf >= 0.30, required=True),
            row("effective reply rate, human labels, relative uplift (alternative reading)", ">= +30 %", "-",
                None if not rf or rs is None else round(rs / rf - 1, 4), None),
            row("effective reply rate, human labels AND sent <= 60 s after the ask", "-",
                h_f["effective_reply_rate_timely"], h_s["effective_reply_rate_timely"], None),
        ]
    else:
        why = ""
        if human is not None:   # a sheet was given but cannot be used: say why (stale, incomplete, ...)
            why = " — labels not usable: " + "; ".join((human.get("problems") or ["incomplete"])[:2])
        human_rows = [row("effective reply rate, 100 human-labelled replies per policy (BRIEF §4)" + why,
                          ">= 0.70 and >= FIFO + 0.30", "pending", "pending", PENDING, required=True)]

    return [
        row("p95 first response, decision -> idle pipeline (s)", "< 3", fr_f, fr_s,
            fr_s is not None and fr_s < 3 and sg["selection"]["selected_unsent"] == 0, required=True),
        row("  ...viewer wait p95, arrival -> sent (s) — what a viewer feels", "-",
            fifo["viewer_wait_s"]["p95"], sg["viewer_wait_s"]["p95"], None),
        *human_rows,
        row("effective reply rate, synthetic gold, 100 sampled (proxy, not acceptance)", "-",
            _q(fifo, "sample100_effective_rate"), _q(sg, "sample100_effective_rate"), None),
        row("effective reply rate, synthetic gold, timely <= 60 s (proxy)", "-",
            _q(fifo, "sample100_effective_rate_timely"), _q(sg, "sample100_effective_rate_timely"), None),
        row(risk_metric, "0", _risk_cell(fifo), _risk_cell(sg), _risk_ok(sg), required=True),
        row("voice channel utilization, confirmed (audio that certainly played)", "0.60 - 0.80",
            fifo["channel"]["utilization"], util_s, _in_range(util_s), required=True),
        row("voice channel utilization, worst case (+ est. audio of failed sends that may have played)",
            "0.60 - 0.80", fifo["channel"].get("utilization_worst_case"), util_worst, _in_range(util_worst),
            required=True),
        row("LLM call ratio (committed generations / valid events)", "<= 0.15", fifo["llm"]["ratio"], llm_s,
            llm_s is not None and llm_s <= 0.15, required=True),
        row("every event has exactly one decision with a reason", "true",
            fifo["explained_every_event"], sg["explained_every_event"], sg["explained_every_event"], required=True),
        row("deterministic (two runs, identical decisions sha256)", "true",
            fifo["deterministic"], sg["deterministic"], sg.get("deterministic"), required=True),
        row("fail open: internal errors", "0", fifo["internal_errors"], sg["internal_errors"],
            sg["internal_errors"] == 0, required=True),
        row("bad input lines dropped and counted", "counted", fifo["input"]["rejected"], sg["input"]["rejected"],
            None),
        row("sink failures handled (replies failed)", "no crash", fifo["selection"]["replies_failed"],
            sg["selection"]["replies_failed"], None),
        row("useful questions answered within 60 s (gold)", "-", _q(fifo, "useful_answered_timely_rate"),
            _q(sg, "useful_answered_timely_rate"), None),
        row("selection rate (events addressed / valid)", "-", fifo["selection"]["selection_rate"],
            sg["selection"]["selection_rate"], None),
        row("never-sent backlog at end", "-", fifo["selection"]["selected_unsent"],
            sg["selection"]["selected_unsent"], None),
        _silence_row(row, fifo, sg),
    ]


def _silence_row(row, fifo: dict, sg: dict) -> dict:
    ch = sg["channel"]
    guard, longest = ch.get("silence_guard_s"), ch["max_silence_s"]
    if guard is None:
        return row("longest silence while live (s)", "-", fifo["channel"]["max_silence_s"], longest, None)
    over = ch.get("silences_over_guard", 0)
    value = longest if not over else f"{longest} ({over} gap(s) > {guard:g} s)"
    return row("longest silence while live (s) — product guard, not BRIEF §4", f"<= {guard:g}",
               fifo["channel"]["max_silence_s"], value, WARN if longest > guard else True)


def verdict(rows: list[dict], note: str | None) -> str:
    if note:
        return "not an acceptance run (smoke test)"
    failed = [r["metric"] for r in rows if r["pass"] is False]
    pending = [r["metric"] for r in rows
               if r["pass"] == PENDING or (r.get("required") and r["pass"] is None)]
    warns = [f"{r['metric']}: {r['streamgate']} (target {r['target']})" for r in rows if r["pass"] == WARN]
    suffix = (" | WARN: " + "; ".join(warns)) if warns else ""
    if failed:
        return "FAIL: " + "; ".join(failed) + suffix
    if pending:
        return "PENDING: automated checks pass; still needed: " + "; ".join(pending) + suffix
    return ("PASS with warnings" + suffix) if warns else "PASS"


def to_markdown(rows: list[dict], title: str, note: str | None = None) -> str:
    def fmt(v):
        if isinstance(v, bool):
            return "yes" if v else "no"
        return "n/a" if v is None else str(v)

    def mark(p):
        return {True: "PASS", False: "FAIL", None: "", PENDING: PENDING, WARN: WARN}[p]

    lines = [f"### {title}", ""]
    if note:
        lines += [f"> **{note}**", ""]
    lines += ["| metric | target | FIFO | Streamgate | pass |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['metric']} | {r['target']} | {fmt(r['fifo'])} | {fmt(r['streamgate'])} | {mark(r['pass'])} |")
    lines += ["", f"**Acceptance: {verdict(rows, note)}**"]
    return "\n".join(lines) + "\n"
