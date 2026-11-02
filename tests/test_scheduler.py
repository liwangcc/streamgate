import pytest

from streamgate.channel import estimate_reply_s
from streamgate.config import Config

from helpers import FailSink, Harness, RefusedSink, ev


@pytest.fixture
def cfg():
    return Config()


def test_same_intent_questions_merge_into_one_reply(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "这个多少钱", ts=0.0))
    h.admit(ev(2, "多少钱", ts=0.6))
    h.run_until(2.0)
    assert len(h.replies) == 1
    r = h.replies[0]
    assert [m.ev.id for m in r.members] == ["e1", "e2"] and r.n_users == 2
    assert "都在问" in r.content
    assert h.record("e1")["outcome"] == "replied"
    m = h.record("e2")
    assert (m["outcome"], m["reason"], m["merged_into"]) == ("merged", "merged", "e1")


def test_gather_window_delays_the_first_decision(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0))
    h.run_until(cfg.gather_s - 0.2)
    assert h.replies == []
    h.run_until(cfg.gather_s + 0.1)
    assert len(h.replies) == 1


def test_decision_and_send_happen_in_the_same_instant(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0))
    h.run_until(5.0)
    rep = [r for r in h.engine.log.records if r["type"] == "reply"][0]
    assert rep["decided_at"] == rep["sent_at"]


def test_different_product_numbers_do_not_merge(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "3号多少钱", ts=0.0))
    h.admit(ev(2, "5号多少钱", ts=0.2))
    h.run_until(1.5)
    assert len(h.replies) == 1 and len(h.replies[0].members) == 1


def test_immediate_drop_reasons(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "保过吗", ts=0.0))
    h.admit(ev(2, "666", ts=0.1))
    h.admit(ev(3, "？？？", ts=0.2))
    assert h.record("e1")["reason"] == "risk"
    assert h.record("e2")["reason"] == "low_score"
    assert h.record("e3")["reason"] == "low_score"
    assert all(r["detail"] for r in h.records())


def test_same_viewer_repeating_is_a_duplicate(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0, user="a"))
    h.admit(ev(2, "多少钱？？", ts=0.3, user="a"))
    assert h.record("e2")["reason"] == "duplicate"


def test_viewer_already_waiting_in_cluster_is_a_duplicate(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0, user="a"))
    h.admit(ev(2, "价格多少", ts=0.3, user="a"))
    assert h.record("e2")["reason"] == "duplicate"


def test_redelivered_event_id_is_a_duplicate(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0, eid="same"))
    h.admit(ev(2, "多少钱", ts=0.1, eid="same", user="b"))
    assert [r["reason"] for r in h.records(id="same")] == ["duplicate"]


def test_recently_answered_viewer_hits_quota(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0, user="a"))
    h.run_until(2.0)
    h.admit(ev(2, "怎么买", ts=10.0, user="a"))
    assert h.record("e2")["reason"] == "quota"
    h.admit(ev(3, "发货吗", ts=1.0 + cfg.user_cooldown_s + 1, user="a"))
    assert not h.records(id="e3", reason="quota")


def test_per_viewer_pending_cap_is_a_quota(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0, user="a"))
    h.admit(ev(2, "怎么买", ts=0.1, user="a"))
    h.admit(ev(3, "发货吗", ts=0.2, user="a"))
    assert h.record("e3")["reason"] == "quota"


def test_pool_overflow_evicts_lowest_priority_as_quota():
    cfg = Config().with_overrides(max_pending=5)
    h = Harness(cfg)
    texts = ["多少钱", "怎么买", "发货吗", "有货吗", "有券吗", "什么材质的", "能演示一下吗"]
    for i, t in enumerate(texts, 1):
        h.admit(ev(i, t, ts=0.01 * i))
        assert h.engine.policy.pending() <= 5
    quota = h.records(reason="quota")
    assert len(quota) == 2
    assert {r["intent"] for r in quota} <= {"product_q", "demo"}


def test_unanswered_question_expires_at_its_ttl(cfg):
    h = Harness(cfg)
    # occupy the channel with a long demo, then let a price question wait past its TTL
    h.admit(ev(1, "能演示一下吗", ts=0.0))
    h.run_until(1.1)
    h.engine.policy.tokens = -1000.0            # no airtime left
    h.admit(ev(2, "多少钱", ts=5.0))
    h.run_until(60.0)
    r = h.record("e2")
    assert (r["outcome"], r["reason"], r["at"]) == ("dropped", "expired", 50.0)


def test_gift_outranks_a_single_question(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0))
    h.admit(ev(2, "送出了玫瑰", ts=0.0, kind="gift", tier="gift", gift=0.1))
    h.run_until(1.5)
    assert h.replies[0].intent == "gift"


def _gift_vs_crowd(cfg, gift_value: float, busy_until: float) -> str:
    """One gift, then 12 viewers asking the price, while the channel is busy until busy_until."""
    h = Harness(cfg)
    h.engine.policy.chan.busy_until = busy_until
    h.admit(ev(1, "送出了小心心", ts=0.0, kind="gift", tier="gift", gift=gift_value))
    for i in range(2, 14):
        h.admit(ev(i, "多少钱", ts=0.1 * i))
    h.run_until(busy_until + 1.0)
    return h.replies[0].intent


def test_small_gift_waits_behind_a_crowd_until_its_max_wait(cfg):
    assert _gift_vs_crowd(cfg, 0.1, busy_until=10.0) == "price"            # crowd wins early on
    assert _gift_vs_crowd(cfg, 0.1, busy_until=25.0) == "gift"             # gift waited >= 20 s
    no_rule = cfg.with_overrides(gift_max_wait_s=1e9)
    assert _gift_vs_crowd(no_rule, 0.1, busy_until=25.0) == "price"


def test_large_gift_beats_a_crowd_immediately(cfg):
    assert _gift_vs_crowd(cfg, 300.0, busy_until=10.0) == "gift"


def test_sends_respect_min_interval_and_airtime_budget(cfg):
    h = Harness(cfg)
    texts = ["多少钱", "怎么买", "发货吗", "有货吗", "有券吗", "能退吗", "能演示一下吗", "什么材质的"]
    seq = 0
    for k in range(600):                          # 10 minutes, a new question every second
        seq += 1
        h.admit(ev(seq, f"{k % 12 + 1}号" + texts[k % len(texts)], ts=float(k)))
    h.run_until(600.0)
    starts = [r.decided_at for r in h.replies]
    assert all(b - a >= cfg.min_send_interval_s - 1e-9 for a, b in zip(starts, starts[1:]))
    airtime = sum(r.est_s for r in h.replies)
    assert airtime <= cfg.target_utilization * 600.0 + cfg.airtime_burst_s + cfg.gift_reserve_s
    assert airtime >= 0.6 * 600.0                 # but it does keep the channel busy


def test_topic_cooldown_lets_another_topic_go_first(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0))
    h.run_until(2.0)
    assert h.replies[-1].intent == "price"
    h.admit(ev(2, "价格多少", ts=5.0))
    h.admit(ev(3, "有货吗", ts=5.0))
    h.run_until(40.0)
    assert h.replies[1].intent == "stock"


def test_welcome_only_when_room_is_quiet(cfg):
    h = Harness(cfg)
    h.admit(ev(1, "主播你好", ts=0.0))
    h.admit(ev(2, "多少钱", ts=0.0))
    h.run_until(1.5)
    assert [r.intent for r in h.replies] == ["price"]        # questions first
    quiet_from = h.engine.policy.chan.busy_until             # estimated end of the price answer
    h.run_until(29.9)
    welcomes = [r for r in h.replies if r.intent == "welcome"]
    assert len(welcomes) == 1
    assert welcomes[0].decided_at >= quiet_from + cfg.warmth_idle_s - 1e-6


def test_ambiguous_failure_does_not_refund_airtime(cfg):
    """SinkResult(ok=False, maybe_delivered=True): it may still play, so its airtime stays spent."""
    h = Harness(cfg, sink=FailSink())
    h.admit(ev(1, "多少钱", ts=0.0))
    h.run_until(1.1)
    rep = h.replies[0]
    assert h.engine.log.records[-2]["maybe_delivered"] is True
    expected = cfg.airtime_burst_s - rep.est_s + 0.1 * cfg.target_utilization   # one refill tick after the send
    assert h.engine.policy.tokens == pytest.approx(expected)


def test_definite_failure_refunds_airtime(cfg):
    """SinkResult(ok=False, maybe_delivered=False): nothing was spoken, so the airtime comes back."""
    h = Harness(cfg, sink=RefusedSink())
    h.admit(ev(1, "多少钱", ts=0.0))
    h.run_until(1.1)
    assert h.engine.policy.tokens == pytest.approx(cfg.airtime_burst_s)


def test_no_refund_unit(cfg):
    """Direct unit check on on_result, without the replay harness."""
    from streamgate.decisions import SinkResult
    h = Harness(cfg)
    h.admit(ev(1, "多少钱", ts=0.0))
    pol = h.engine.policy
    reply = pol.tick(1.0)
    after_send = pol.tokens
    pol.on_result(reply, SinkResult(ok=False, at=3.0, error="timeout", maybe_delivered=True), 3.0)
    assert pol.tokens == after_send
    h2 = Harness(cfg)
    h2.admit(ev(1, "多少钱", ts=0.0))
    pol2 = h2.engine.policy
    reply2 = pol2.tick(1.0)
    pol2.on_result(reply2, SinkResult(ok=False, at=1.0, error="refused", maybe_delivered=False), 1.0)
    assert pol2.tokens == pytest.approx(pol.tokens + reply.est_s)


def test_timeout_keeps_the_channel_reserved_but_refused_frees_it(cfg):
    timed_out = Harness(cfg, sink=FailSink())
    refused = Harness(cfg, sink=RefusedSink())
    for h in (timed_out, refused):
        h.admit(ev(1, "多少钱", ts=0.0))
        h.run_until(1.1)
    assert timed_out.engine.policy.chan.busy_until > 1.0          # it may still be playing
    assert refused.engine.policy.chan.busy_until < 0              # back to idle
    for h in (timed_out, refused):
        h.admit(ev(2, "怎么买", ts=2.0))
        h.run_until(9.5)
    assert len(refused.replies) == 2 and refused.replies[1].decided_at == pytest.approx(9.0)  # min interval
    assert len(timed_out.replies) == 1                            # waits for the reserved airtime


def test_sink_failure_is_recorded_and_backs_off(cfg):
    h = Harness(cfg, sink=FailSink())
    h.admit(ev(1, "多少钱", ts=0.0))
    h.run_until(1.1)
    assert h.record("e1")["reason"] == "sink_error"
    rep = [r for r in h.engine.log.records if r["type"] == "reply"][0]
    assert rep["ok"] is False and rep["maybe_delivered"] is True
    assert h.engine.policy.chan.backoff_until == pytest.approx(1.0 + 2.0 + cfg.sink_backoff_s)
    h.admit(ev(2, "怎么买", ts=2.0))
    h.run_until(7.9)
    assert len(h.replies) == 1                     # still backing off
    assert h.engine.internal_errors == 0


def test_policy_bug_fails_open(cfg):
    h = Harness(cfg)

    def boom(*a, **k):
        raise RuntimeError("bug")
    h.engine.policy.admit = boom
    h.admit(ev(1, "多少钱", ts=0.0))
    assert h.engine.internal_errors == 1
    assert h.record("e1")["reason"] == "internal_error"


def test_every_event_gets_exactly_one_final_record(cfg):
    h = Harness(cfg)
    texts = ["多少钱", "多少钱", "保过吗", "666", "主播你好", "怎么买", "能演示一下吗", "什么材质的"]
    for i, t in enumerate(texts * 5, 1):
        h.admit(ev(i, t, ts=0.4 * i, user=f"u{i % 7}"))
    h.run_until(200.0)
    h.engine.finish(200.0)
    ids = [r["id"] for r in h.records()]
    assert sorted(ids) == sorted(f"e{i}" for i in range(1, 41))


def test_reply_estimate_is_clamped(cfg):
    assert estimate_reply_s(cfg, "gift", 1) == cfg.min_reply_s
    assert estimate_reply_s(cfg, "demo", 50) <= cfg.max_reply_s
    assert estimate_reply_s(cfg, "price", 3) > estimate_reply_s(cfg, "price", 1)
