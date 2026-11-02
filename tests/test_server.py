import json
import threading
import urllib.error
import urllib.request

import pytest

from streamgate.clock import ManualClock
from streamgate.config import Config
from streamgate.decisions import SinkResult
from streamgate.server import MAX_BODY, LiveService, make_server


class LockCheckingSink:
    """Records sends and asserts the service lock is NOT held while sending."""

    def __init__(self):
        self.service = None
        self.sent = []

    def send(self, reply, now):
        assert not self.service.lock.locked(), "sink called while holding the ingest lock"
        self.sent.append(reply)
        return SinkResult(True, now)


@pytest.fixture
def svc(tmp_path):
    sink = LockCheckingSink()
    clock = ManualClock(100.0)
    s = LiveService(Config(), "streamgate", sink, clock, str(tmp_path / "decisions.jsonl"))
    sink.service = s
    yield s, clock, sink
    s.close()


def _step_until(svc_tuple, t):
    s, clock, _ = svc_tuple
    while clock.now() < t:
        clock.advance(0.1)
        s.step()


def test_ingest_accepts_object_array_and_jsonl(svc):
    s, clock, sink = svc
    one = {"id": "a", "user_id": "u1", "text": "多少钱"}
    assert s.ingest_body(json.dumps(one, ensure_ascii=False).encode()) == {"accepted": 1, "rejected": 0}
    arr = [{"id": "b", "user_id": "u2", "text": "多少钱"}, {"id": "c", "text": 5}, 7]
    assert s.ingest_body(json.dumps(arr).encode()) == {"accepted": 1, "rejected": 2}
    jsonl = b'{"id": "d", "user_id": "u3", "text": "\xe6\x80\x8e\xe4\xb9\x88\xe4\xb9\xb0"}\nnot json\n\n'
    assert s.ingest_body(jsonl) == {"accepted": 1, "rejected": 1}
    assert s.ingest_body(b"\xff\xfe") == {"accepted": 0, "rejected": 1}
    _step_until(svc, 102.0)
    assert len(sink.sent) == 1 and sink.sent[0].n_users == 2      # 多少钱 x2 merged, sent outside the lock
    snap = s.snapshot()
    assert snap["events"] == 7 and snap["outcomes"]["dropped/malformed"] == 4
    assert snap["internal_errors"] == 0


class FlakySink:
    """Fails (timeout) while ``down`` is True."""

    def __init__(self):
        self.down = True

    def send(self, reply, now):
        if self.down:
            return SinkResult(False, now, "timeout", maybe_delivered=True)
        return SinkResult(True, now)


def test_live_alerts_when_sends_keep_failing():
    """REVIEW M4 P2: the host must see when the voice pipeline stalls, because the room goes quiet."""
    sink, clock = FlakySink(), ManualClock(0.0)
    s = LiveService(Config(), "streamgate", sink, clock)
    assert s.snapshot()["alerts"] == []
    texts = ["多少钱", "怎么买", "发货吗", "有货吗", "有券吗"]
    for i, t in enumerate(texts):
        s.ingest_obj({"id": f"a{i}", "user_id": f"u{i}", "text": t})
    while s.consecutive_failures < 2 and clock.now() < 120:
        clock.advance(0.1)
        s.step()
    snap = s.snapshot()
    assert snap["consecutive_send_failures"] == 2 and snap["last_ok_send_age_s"] is None
    assert any("consecutive failed sends" in a for a in snap["alerts"])
    assert not any("no confirmed send" in a for a in snap["alerts"])     # not yet 30 s
    clock.advance(Config().silence_guard_s + 1)                          # still nothing confirmed
    assert s.engine.pending() > 0
    assert any("no confirmed send" in a for a in s.snapshot()["alerts"])
    sink.down = False
    s.ingest_obj({"id": "b1", "user_id": "x", "text": "能演示一下吗"})
    while s.consecutive_failures and clock.now() < 300:
        clock.advance(0.1)
        s.step()
    snap = s.snapshot()
    assert snap["consecutive_send_failures"] == 0 and snap["last_ok_send_age_s"] is not None
    assert not any("consecutive failed sends" in a for a in snap["alerts"])


def test_live_mode_uses_server_arrival_time(svc):
    s, clock, _ = svc
    s.ingest_body(json.dumps({"id": "a", "text": "多少钱", "ts": -999}).encode())
    rec = [r for r in s.recent(10)]
    assert rec == []                                                # still pending, ts was ignored
    _step_until(svc, 102.0)
    reply = [r for r in s.recent(10) if r["type"] == "reply"][0]
    assert reply["lead_ts"] == 100.0


def test_decisions_are_written_to_disk(svc, tmp_path):
    s, clock, _ = svc
    s.ingest_body(json.dumps({"id": "a", "text": "保过吗"}).encode())
    s.flush()                                                        # written by the writer thread
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["reason"] == "risk"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_refuses_non_loopback_bind(svc, host):
    with pytest.raises(ValueError):
        make_server(svc[0], host, 0)


def test_http_round_trip(svc):
    s, clock, _ = svc
    srv = make_server(s, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, headers=headers or {},
                                     method="POST" if body is not None else "GET")
        try:
            with op.open(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    try:
        assert call("/healthz") == (200, {"ok": True})
        code, body = call("/ingest", json.dumps([{"id": "a", "text": "多少钱"}, {"bad": 1}]).encode(),
                          {"Content-Type": "application/json"})
        assert code == 200 and body == {"accepted": 1, "rejected": 1}
        assert call("/ingest", b"garbage")[0] == 200
        assert call("/nope")[0] == 404
        try:   # server answers 413 without reading the body; the OS may abort the still-sending client
            assert call("/ingest", b"x" * (MAX_BODY + 1))[0] == 413
        except (ConnectionError, urllib.error.URLError):
            pass
        assert s.stats["body_too_large"] == 1
        assert call("/healthz") == (200, {"ok": True})               # and keeps serving
        code, m = call("/metrics")
        assert code == 200 and m["events"] == 3 and m["http_errors"] == 0
        assert call("/decisions?limit=abc")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()
