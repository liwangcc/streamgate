from streamgate.config import Config
from streamgate.metrics import _max_overlap, compute, percentile
from streamgate.replay import ReplayResult


def test_percentile_interpolates():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(xs, 50) == 5.5
    assert abs(percentile(xs, 95) - 9.55) < 1e-9
    assert percentile([], 95) is None and percentile([3.0], 95) == 3.0


def test_max_overlap():
    assert _max_overlap([(0, 1), (1, 2)]) == 1          # touching intervals do not overlap
    assert _max_overlap([(0, 3), (1, 2), (1.5, 4)]) == 3
    assert _max_overlap([]) == 0


def _reply(rid, sent, start, end, decided=None):
    return {"type": "reply", "reply_id": rid, "lead_id": rid, "lead_ts": 0.0, "member_ids": [rid],
            "decided_at": sent if decided is None else decided, "sent_at": sent, "ok": True,
            "audio_start": start, "audio_end": end, "downstream_wait_s": 0.0}


def _event(seq, outcome="replied", reason="selected", ts=0.0, at=1.0):
    return {"type": "event", "seq": seq, "id": f"e{seq}", "ts": ts, "kind": "comment", "intent": "price",
            "outcome": outcome, "reason": reason, "at": at}


def test_utilization_silence_and_first_response():
    records = [_event(1), _event(2), _reply("a", 0.0, 2.0, 10.0), _reply("b", 20.0, 22.0, 30.0, decided=19.0)]
    res = ReplayResult("x", records, t0=0.0, t_last=40.0, horizon=100.0, lines=2, blank_lines=0,
                       internal_errors=0)
    m = compute(res, Config())
    assert m["channel"]["utilization"] == round(16 / 40, 4)
    assert m["channel"]["max_silence_s"] == 12.0          # 10 -> 22
    assert m["first_response_s"]["max"] == 1.0
    assert m["explained_every_event"]


def test_missing_decision_is_detected():
    res = ReplayResult("x", [_event(1)], t0=0.0, t_last=1.0, horizon=2.0, lines=2, blank_lines=0,
                       internal_errors=0)
    assert compute(res, Config())["explained_every_event"] is False
