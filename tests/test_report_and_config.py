import pytest

from streamgate.config import Config, ConfigError
from streamgate.metrics import compute, load_gold
from streamgate.replay import ReplayResult, read_lines, run_replay
from streamgate.report import PENDING, WARN, acceptance, scale_note, to_markdown, verdict
from streamgate.sim import SimConfig

from test_replay import DATA, SYNTH

CFG = Config()


@pytest.fixture(scope="module")
def synth():
    lines, gold = read_lines(SYNTH), load_gold(DATA / "replay.synth30.gold.jsonl")
    out = {}
    for p in ("fifo", "streamgate"):
        m = compute(run_replay(lines, p, CFG), CFG, gold)
        m["deterministic"] = True
        out[p] = m
    return out


def _row(rows, prefix):
    found = [r for r in rows if r["metric"].startswith(prefix)]
    assert len(found) == 1, prefix
    return found[0]


def test_acceptance_is_pending_until_humans_label(synth):
    rows = acceptance(synth["fifo"], synth["streamgate"])
    assert _row(rows, "effective reply rate, 100 human")["pass"] == PENDING
    assert _row(rows, "effective reply rate, synthetic gold, 100")["pass"] is None   # a proxy never decides
    assert scale_note(synth["streamgate"]) is None                                  # 30 min = acceptance scale
    assert verdict(rows, None).startswith("PENDING")


def test_risk_row_is_a_hard_check(synth):
    def with_quality(**kw):
        return dict(synth["streamgate"], quality=dict(synth["streamgate"]["quality"], **kw))

    rows = acceptance(synth["fifo"], with_quality(risk_leaks=1))
    assert _row(rows, "risk messages voiced")["pass"] is False
    assert verdict(rows, None).startswith("FAIL") and "risk" in verdict(rows, None)
    assert _row(acceptance(synth["fifo"], with_quality(risk_leaks_unconfirmed=1)),
                "risk messages voiced")["pass"] is False
    assert _row(acceptance(synth["fifo"], synth["streamgate"]), "risk messages voiced")["pass"] is True


def test_human_labels_decide_the_effective_rate_row(synth):
    human = {"complete": True, "per_policy": {
        "fifo": {"labeled": 100, "effective_reply_rate": 0.35, "effective_reply_rate_timely": 0.0},
        "streamgate": {"labeled": 100, "effective_reply_rate": 0.62, "effective_reply_rate_timely": 0.6}}}
    rows = acceptance(synth["fifo"], synth["streamgate"], human)
    assert _row(rows, "effective reply rate, human labels (")["pass"] is False      # 0.62 < 0.70
    human["per_policy"]["streamgate"]["effective_reply_rate"] = 0.8
    rows = acceptance(synth["fifo"], synth["streamgate"], human)
    assert _row(rows, "effective reply rate, human labels (")["pass"] is True
    assert verdict(rows, None) == "PASS"


def test_incomplete_human_labels_stay_pending(synth):
    human = {"complete": False, "per_policy": {
        "fifo": {"labeled": 10, "effective_reply_rate": 0.1, "effective_reply_rate_timely": 0.0},
        "streamgate": {"labeled": 10, "effective_reply_rate": 1.0, "effective_reply_rate_timely": 1.0}}}
    assert _row(acceptance(synth["fifo"], synth["streamgate"], human),
                "effective reply rate, 100 human")["pass"] == PENDING


COMPLETE_HUMAN = {"complete": True, "per_policy": {
    "fifo": {"labeled": 100, "effective_reply_rate": 0.30, "effective_reply_rate_timely": 0.0},
    "streamgate": {"labeled": 100, "effective_reply_rate": 0.90, "effective_reply_rate_timely": 0.9}}}


def test_no_gold_with_complete_human_labels_is_pending_never_pass(synth):
    """REVIEW M3 P1: without gold, "0 risk voiced" is unverifiable, so complete labels must not give PASS."""
    no_gold = {p: {k: v for k, v in m.items() if k != "quality"} for p, m in synth.items()}
    rows = acceptance(no_gold["fifo"], no_gold["streamgate"], COMPLETE_HUMAN)
    risk = _row(rows, "risk messages voiced")
    assert risk["pass"] == PENDING and "gold labels missing" in risk["metric"] and risk["streamgate"] == "unverified"
    v = verdict(rows, None)
    assert v.startswith("PENDING") and "gold" in v and v != "PASS"
    # the only difference to a PASS is the gold labels
    assert verdict(acceptance(synth["fifo"], synth["streamgate"], COMPLETE_HUMAN), None) == "PASS"


def test_required_row_without_a_result_is_pending_not_informational(synth):
    sg = dict(synth["streamgate"], deterministic=None)
    rows = acceptance(synth["fifo"], sg, COMPLETE_HUMAN)
    assert _row(rows, "deterministic")["pass"] is None
    assert verdict(rows, None).startswith("PENDING") and "deterministic" in verdict(rows, None)


def _fault_run(rate):
    lines, gold = read_lines(SYNTH), load_gold(DATA / "replay.synth30.gold.jsonl")
    sim = SimConfig(sink_fail_rate=rate)
    out = {}
    for p in ("fifo", "streamgate"):
        res = run_replay(lines, p, CFG, sim)
        m = compute(res, CFG, gold)
        m["deterministic"] = True
        out[p] = (res, m)
    return out


def test_five_percent_timeouts_stay_in_band_confirmed_and_worst_case():
    ch = _fault_run(0.05)["streamgate"][1]["channel"]
    assert 0.60 <= ch["utilization"] <= ch["utilization_worst_case"] <= 0.80


def test_twenty_percent_timeouts_report():
    """REVIEW M3 P2-1: the report shows confirmed AND worst-case utilization and never passes on
    confirmed alone; with no refund for ambiguous timeouts the worst case stays within budget."""
    runs = _fault_run(0.20)
    res, m = runs["streamgate"]
    ch = m["channel"]
    failed_maybe = [r for r in res.records if r["type"] == "reply" and not r["ok"] and r["maybe_delivered"]]
    assert failed_maybe, "the drill must contain ambiguous failures"
    assert ch["maybe_played_s"] == pytest.approx(sum(r["est_s"] for r in failed_maybe), abs=1e-3)
    assert ch["utilization_worst_case"] == pytest.approx((ch["busy_s"] + ch["maybe_played_s"]) / ch["span_s"],
                                                         abs=1e-4)
    assert ch["utilization_worst_case"] > ch["utilization"]
    assert ch["utilization_worst_case"] <= 0.80          # token bucket charges sends that may have played

    rows = acceptance(runs["fifo"][1], m, COMPLETE_HUMAN)
    confirmed = _row(rows, "voice channel utilization, confirmed")
    worst = _row(rows, "voice channel utilization, worst case")
    assert worst["streamgate"] == ch["utilization_worst_case"]
    for r, u in ((confirmed, ch["utilization"]), (worst, ch["utilization_worst_case"])):
        assert r["pass"] is (0.60 <= u <= 0.80) and r["required"]
    v = verdict(rows, None)
    required_ok = all(r["pass"] is True for r in rows if r["required"])
    assert (v == "PASS") is required_ok
    if not confirmed["pass"] or not worst["pass"]:
        assert v.startswith("FAIL") and "utilization" in v


def test_silence_guard_row_and_verdict(synth):
    """REVIEW M4 P2: a long silence is a visible WARN, appended to the verdict, never a bare PASS."""
    rows = acceptance(synth["fifo"], synth["streamgate"], COMPLETE_HUMAN)
    silence = _row(rows, "longest silence while live")
    assert silence["pass"] is True and synth["streamgate"]["channel"]["max_silence_s"] <= 30   # clean run
    assert verdict(rows, None) == "PASS"

    loud = dict(synth["streamgate"], channel=dict(synth["streamgate"]["channel"],
                                                  max_silence_s=55.8, silences_over_guard=1))
    rows = acceptance(synth["fifo"], loud, COMPLETE_HUMAN)
    assert _row(rows, "longest silence while live")["pass"] == WARN
    v = verdict(rows, None)
    assert v.startswith("PASS with warnings") and "55.8" in v and v != "PASS"
    rows = acceptance(synth["fifo"], loud)                       # still pending human labels
    assert "| WARN:" in verdict(rows, None) and verdict(rows, None).startswith("PENDING")
    assert "WARN" in to_markdown(rows, "t")


def test_five_percent_timeouts_warn_about_silence():
    runs = _fault_run(0.05)
    ch = runs["streamgate"][1]["channel"]
    rows = acceptance(runs["fifo"][1], runs["streamgate"][1])
    row = _row(rows, "longest silence while live")
    assert (row["pass"] == WARN) is (ch["max_silence_s"] > ch["silence_guard_s"])
    assert ch["silences_over_guard"] >= (1 if ch["max_silence_s"] > ch["silence_guard_s"] else 0)


def test_silence_guard_must_be_positive():
    with pytest.raises(ConfigError, match="silence_guard_s"):
        Config().with_overrides(silence_guard_s=0)


def test_small_replay_is_flagged_as_smoke_test():
    res = run_replay(read_lines(DATA / "replay.sample.jsonl"), "streamgate", CFG)
    note = scale_note(compute(res, CFG))
    assert note and note.startswith("SMOKE TEST")
    assert "SMOKE TEST" in to_markdown([], "t", note)
    assert verdict([{"metric": "x", "pass": False}], note).startswith("not an acceptance run")


def test_failed_sends_with_risky_members_are_counted_as_unconfirmed():
    gold = {"a": {"risk": True}}
    recs = [{"type": "event", "seq": 1, "id": "a", "ts": 0.0, "kind": "comment", "intent": "product_q",
             "outcome": "dropped", "reason": "sink_error", "at": 2.0},
            {"type": "reply", "reply_id": "r1", "lead_id": "a", "lead_ts": 0.0, "member_ids": ["a"],
             "decided_at": 0.0, "sent_at": None, "ok": False}]
    res = ReplayResult("x", recs, 0.0, 1.0, 2.0, lines=1, blank_lines=0, internal_errors=0)
    q = compute(res, CFG, gold)["quality"]
    assert q["risk_leaks"] == 0 and q["risk_leaks_unconfirmed"] == 1


def test_fault_run_voices_no_risk_either():
    res = run_replay(read_lines(SYNTH), "streamgate", CFG, SimConfig(sink_fail_rate=0.05))
    q = compute(res, CFG, load_gold(DATA / "replay.synth30.gold.jsonl"))["quality"]
    assert q["risk_leaks"] == 0 and q["risk_leaks_unconfirmed"] == 0


@pytest.mark.parametrize("kw", [dict(target_utilization=0), dict(target_utilization=1.01),
                                dict(max_pending=10.5), dict(max_pending=0), dict(tick_s=-0.1),
                                dict(gather_s=float("inf")), dict(min_reply_s=30.0), dict(max_names=True),
                                dict(topic_cooldown_factor=2.0), dict(extra_risk_words=("a", 1))])
def test_config_rejects_bad_values(kw):
    with pytest.raises(ConfigError):
        Config().with_overrides(**kw)


@pytest.mark.parametrize("field", ["min_send_interval_s", "fifo_min_send_interval_s"])
@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_send_intervals_must_exceed_aivtuber_window(field, value):
    """REVIEW M3 P2-2 / BRIEF §5: AI-Vtuber keeps only the last message per 1 s, so sends must be > 1 s apart."""
    with pytest.raises(ConfigError, match=field):
        Config().with_overrides(**{field: value})


@pytest.mark.parametrize("field", ["min_send_interval_s", "fifo_min_send_interval_s"])
def test_send_intervals_just_above_the_window_are_accepted(field):
    assert getattr(Config().with_overrides(**{field: 1.1}), field) == 1.1


def test_send_interval_defaults_are_unchanged():
    assert Config().min_send_interval_s == 8.0 and Config().fifo_min_send_interval_s == 1.1


def test_send_interval_is_validated_when_loading_a_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"min_send_interval_s": 0}', encoding="utf-8")
    with pytest.raises(ConfigError, match="min_send_interval_s"):
        Config.load(p)


def test_config_load_validates_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"gift_max_wait_s": 30, "extra_risk_words": ["长高"]}', encoding="utf-8")
    c = Config.load(p)
    assert c.gift_max_wait_s == 30 and c.extra_risk_words == ("长高",)
    for body in ('{"nope": 1}', "[1]", "not json", '{"max_pending": "300"}'):
        p.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(p)
