from pathlib import Path

import pytest

from streamgate.config import Config
from streamgate.metrics import compute, load_gold
from streamgate.replay import read_lines, run_replay
from streamgate.sim import SimConfig

from helpers import line

DATA = Path(__file__).resolve().parent.parent / "data"
SAMPLE = DATA / "replay.sample.jsonl"
SYNTH = DATA / "replay.synth30.jsonl"
CFG = Config()


def _events(res):
    return {r["id"]: r for r in res.records if r["type"] == "event"}


def test_sample_streamgate_decisions():
    res = run_replay(read_lines(SAMPLE), "streamgate", CFG)
    ev = _events(res)
    assert ev["c002"]["merged_into"] == "c001"                  # 这个多少钱 + 多少钱
    assert ev["c012"]["merged_into"] == "c011"                  # 怎么买 + 在哪里买
    assert ev["c008"]["outcome"] == "replied"                   # gift thanked
    assert ev["c010"]["reason"] == "risk"                       # 保过吗
    assert ev["c005"]["reason"] == ev["c009"]["reason"] == "low_score"
    assert all(r["reason"] for r in ev.values()) and len(ev) == 12


def test_sample_fifo_strands_the_gift():
    res = run_replay(read_lines(SAMPLE), "fifo", CFG)
    assert _events(res)["c008"]["outcome"] == "selected_unsent"


@pytest.mark.parametrize("policy", ["fifo", "streamgate"])
def test_replay_is_deterministic(policy):
    lines = read_lines(SYNTH)
    a = run_replay(lines, policy, CFG)
    b = run_replay(list(lines), policy, CFG)
    assert a.sha256 == b.sha256


def test_out_of_order_and_blank_lines():
    lines = [line(id="b", ts=2.0, text="怎么买"), b"", line(id="a", ts=1.0, text="多少钱"), b"   "]
    res = run_replay(lines, "streamgate", CFG)
    assert res.blank_lines == 2 and res.t0 == 1.0
    m = compute(res, CFG)
    assert m["input"]["events"] == 2 and m["explained_every_event"]


def test_empty_replay_does_not_crash():
    res = run_replay([], "streamgate", CFG)
    assert res.records == [] and compute(res, CFG)["input"]["events"] == 0


GARBAGE = [
    b"not json", b"[1,2]", b"null", b"\xff\xfe", b'{"id": "t", "text": ',
    line(id="g1", text=None), line(id="g2", text=123), line(id="g3", text="   "),
    line(id="g4", text="买" * 10000), line(id="g5", ts="abc"), line(id="g6", ts=-1),
    line(id="g7", kind="like"), line(id="g8", kind="gift", text="", gift_value="lots"),
    line(id="ok1", ts=3.0), line(id="ok1", ts=3.1), b'{"id": "n", "ts": NaN, "text": "x"}',
    b'{"text": "no id no user", "ts": 4.0}', line(id="ok2", ts=5.0, text="多少钱"),
]


@pytest.mark.parametrize("policy", ["fifo", "streamgate"])
def test_garbage_replay_fails_open(policy):
    res = run_replay(GARBAGE, policy, CFG)
    m = compute(res, CFG)
    assert m["internal_errors"] == 0 and m["explained_every_event"]
    assert m["input"]["events"] == len(GARBAGE)
    assert m["outcomes"]["dropped"] >= 9
    assert m["drop_reasons"]["malformed"] == 11 and m["drop_reasons"]["unsupported"] == 1


def test_sink_failures_are_survived_and_explained():
    sim = SimConfig(sink_fail_rate=0.2)
    res = run_replay(read_lines(SYNTH), "streamgate", CFG, sim)
    m = compute(res, CFG)
    assert m["internal_errors"] == 0 and m["explained_every_event"]
    assert m["selection"]["replies_failed"] > 0 and m["drop_reasons"]["sink_error"] > 0


@pytest.fixture(scope="module")
def synth_metrics():
    lines, gold = read_lines(SYNTH), load_gold(DATA / "replay.synth30.gold.jsonl")
    return {p: compute(run_replay(lines, p, CFG), CFG, gold) for p in ("fifo", "streamgate")}


def test_acceptance_on_30_minute_replay(synth_metrics):
    """BRIEF §4 on the synthetic replay (the effective-rate numbers are an optimistic proxy, see DEVLOG)."""
    f, s = synth_metrics["fifo"], synth_metrics["streamgate"]
    assert s["first_response_s"]["p95"] < 3.0 and s["selection"]["selected_unsent"] == 0
    assert f["first_response_s"]["p95"] > 60.0                       # the baseline really is broken
    eff_s, eff_f = s["quality"]["sample100_effective_rate"], f["quality"]["sample100_effective_rate"]
    assert eff_s >= 0.70 and eff_s - eff_f >= 0.30
    assert 0.60 <= s["channel"]["utilization"] <= 0.80
    assert s["llm"]["ratio"] <= 0.15 and s["llm"]["scoring_calls"] == 0
    assert s["explained_every_event"] and s["internal_errors"] == 0
    assert s["channel"]["max_in_pipeline"] <= 1                      # never stacks AI-Vtuber's queue
    assert s["channel"]["min_send_gap_s"] > 1.0                      # AI-Vtuber's 1 s merge window


def test_streamgate_answers_more_useful_questions_in_time(synth_metrics):
    f, s = synth_metrics["fifo"]["quality"], synth_metrics["streamgate"]["quality"]
    assert s["useful_answered_timely_rate"] > 10 * max(f["useful_answered_timely_rate"], 0.01)
    assert f["risk_leaks"] > 0
    assert s["risk_leaks"] == 0 and s["risk_leaks_unconfirmed"] == 0     # no risky message is voiced


def test_backlog_stays_bounded(synth_metrics):
    pend_s = [r["pending_at_end"] for r in synth_metrics["streamgate"]["timeline"] if r["pending_at_end"] is not None]
    pend_f = [r["pending_at_end"] for r in synth_metrics["fifo"]["timeline"] if r["pending_at_end"] is not None]
    assert max(pend_s) <= CFG.max_pending and max(pend_s) < 150
    assert pend_f == sorted(pend_f) and pend_f[-1] > 2000           # FIFO grows without bound
