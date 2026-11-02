from streamgate.config import Config

from helpers import Harness, ev


def test_fifo_answers_everything_in_arrival_order_and_decides_on_arrival():
    cfg = Config()
    h = Harness(cfg, policy="fifo")
    texts = ["666", "保过吗", "多少钱", "主播你好"]
    for i, t in enumerate(texts, 1):
        h.admit(ev(i, t, ts=0.5 * i))
    h.run_until(200.0)
    assert [r.lead.id for r in h.replies] == ["e1", "e2", "e3", "e4"]
    assert [r.decided_at for r in h.replies] == [0.5, 1.0, 1.5, 2.0]
    assert not h.records(outcome="dropped")


def test_fifo_backlog_is_reported_as_selected_unsent():
    cfg = Config()
    h = Harness(cfg, policy="fifo")
    for i in range(1, 31):
        h.admit(ev(i, "多少钱", ts=0.1 * i))
    h.run_until(30.0)
    h.engine.finish(30.0)
    unsent = h.records(outcome="selected_unsent")
    assert unsent and all(r["reason"] == "backlog" for r in unsent)
    assert len(h.replies) + len(unsent) == 30


def test_fifo_never_overlaps_audio_on_its_own_estimate():
    cfg = Config()
    h = Harness(cfg, policy="fifo")
    for i in range(1, 11):
        h.admit(ev(i, "多少钱", ts=0.01 * i))
    h.run_until(150.0)
    reps = [r for r in h.engine.log.records if r["type"] == "reply"]
    assert len(reps) == 10
    for a, b in zip(reps, reps[1:]):   # next send waits for the previous estimated audio end
        assert b["sent_at"] >= a["sent_at"] + cfg.prep_est_s + a["est_s"] - cfg.pipeline_lead_s - 1e-3
