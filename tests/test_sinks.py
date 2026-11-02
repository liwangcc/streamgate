import http.server
import json
import socket
import threading

import pytest

from streamgate.clock import ManualClock
from streamgate.decisions import Member, Reply
from streamgate.rules import classify
from streamgate.sinks import DryRunSink, HttpSink, aivtuber_payload, is_loopback

from helpers import ev


def _reply(*texts):
    members = [Member(e, classify(e)) for e in (ev(i, t) for i, t in enumerate(texts, 1))]
    return Reply("r00001", "streamgate", members[0].cls.intent, None, members, 0.0, 9.0, 60.0, "why")


class FakeResp:
    def __init__(self, status):
        self.status = status

    def read(self, n=-1):
        return b"{}"


class FakeConn:
    """Stands in for http.client.HTTPConnection; can fail in the connect, request or response phase."""

    def __init__(self, status=200, connect_exc=None, request_exc=None, response_exc=None):
        self.status, self.connect_exc, self.request_exc, self.response_exc = status, connect_exc, request_exc, response_exc
        self.sent, self.closed = None, False

    def connect(self):
        if self.connect_exc:
            raise self.connect_exc

    def request(self, method, path, body=None, headers=None):
        if self.request_exc:
            raise self.request_exc
        self.sent = {"method": method, "path": path, "body": json.loads(body), "headers": headers}

    def getresponse(self):
        if self.response_exc:
            raise self.response_exc
        return FakeResp(self.status)

    def close(self):
        self.closed = True


def _sink(conn, url="http://127.0.0.1:8082/send", clock=None):
    return HttpSink(url, clock or ManualClock(), connection_factory=lambda: conn)


@pytest.mark.parametrize("host,ok", [("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
                                     ("127.0.0.2", True), ("0.0.0.0", False), ("10.0.0.1", False),
                                     ("example.com", False), ("", False), (None, False)])
def test_is_loopback(host, ok):
    assert is_loopback(host) is ok


@pytest.mark.parametrize("url", ["http://example.com/send", "http://10.0.0.5:8082/send", "ftp://127.0.0.1/x",
                                 "127.0.0.1:8082/send"])
def test_http_sink_refuses_non_local_urls(url):
    with pytest.raises(ValueError):
        HttpSink(url, ManualClock())


def test_payload_matches_aivtuber_send():
    p = aivtuber_payload(_reply("这个多少钱", "多少钱"))
    assert p == {"type": "comment", "data": {"username": "n1", "content": "这个多少钱（n1、n2都在问）",
                                             "platform": "douyin"}}


def test_http_sink_success_and_request_shape():
    conn = FakeConn(200)
    r = _sink(conn, "http://127.0.0.1:8082/send?x=1", ManualClock(5.0)).send(_reply("多少钱"), 5.0)
    assert r.ok and r.at == 5.0 and conn.closed
    assert conn.sent["method"] == "POST" and conn.sent["path"] == "/send?x=1"
    assert conn.sent["body"]["type"] == "comment" and "application/json" in conn.sent["headers"]["Content-Type"]


def test_default_connection_uses_the_url_and_timeout():
    sink = HttpSink("http://127.0.0.1:8082/send", ManualClock(), timeout_s=1.5)
    conn = sink._connection()
    assert (conn.host, conn.port, conn.timeout) == ("127.0.0.1", 8082, 1.5)


@pytest.mark.parametrize("phase,exc,maybe", [
    # never connected: AI-Vtuber cannot have the reply (Windows reports a closed localhost port as a
    # connect *timeout* when the timeout is short, so a connect timeout is definite too)
    ("connect", ConnectionRefusedError("refused"), False),
    ("connect", socket.timeout("timed out"), False),
    ("connect", TimeoutError("timed out"), False),
    ("connect", OSError("unreachable"), False),
    # connected, then trouble: the request may have reached AI-Vtuber and may still play
    ("request", ConnectionResetError("reset"), True),
    ("request", BrokenPipeError("pipe"), True),
    ("response", socket.timeout("timed out"), True),
    ("response", TimeoutError("timed out"), True),
    ("response", ValueError("weird"), True),
])
def test_failure_phase_decides_maybe_delivered(phase, exc, maybe):
    conn = FakeConn(**{f"{phase}_exc": exc})
    r = _sink(conn).send(_reply("多少钱"), 0.0)
    assert r.ok is False and r.maybe_delivered is maybe and r.error and conn.closed


def test_factory_failure_is_definite_and_never_raises():
    def boom():
        raise RuntimeError("cannot build connection")
    r = HttpSink("http://127.0.0.1:8082/send", ManualClock(), connection_factory=boom).send(_reply("多少钱"), 0.0)
    assert (r.ok, r.maybe_delivered) == (False, False)


@pytest.mark.parametrize("status", [400, 500, 503])
def test_http_error_status_is_a_definite_failure(status):
    r = _sink(FakeConn(status)).send(_reply("多少钱"), 0.0)
    assert (r.ok, r.error, r.maybe_delivered) == (False, f"HTTP {status}", False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_closed_port_is_a_definite_failure():
    """Nothing listens: refused (Linux) or connect timeout (Windows) -- either way not delivered."""
    r = HttpSink(f"http://127.0.0.1:{_free_port()}/send", ManualClock(), timeout_s=0.5).send(_reply("多少钱"), 0.0)
    assert (r.ok, r.maybe_delivered) == (False, False) and r.error.startswith("connect failed")


def test_real_silent_listener_is_ambiguous():
    """It accepts the TCP connection but never answers: the request may have been taken."""
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        r = HttpSink(f"http://127.0.0.1:{port}/send", ManualClock(), timeout_s=0.3).send(_reply("多少钱"), 0.0)
    assert (r.ok, r.maybe_delivered) == (False, True)


def test_real_http_server_round_trip():
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got["path"] = self.path
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code = 503 if got["body"]["data"]["content"] == "busy" else 200
            self.send_response(code)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        sink = HttpSink(f"http://127.0.0.1:{srv.server_port}/send", ManualClock(), timeout_s=2.0)
        assert sink.send(_reply("多少钱"), 0.0).ok
        assert got["path"] == "/send" and got["body"]["data"]["content"] == "多少钱"
        r = sink.send(_reply("busy"), 0.0)
        assert (r.ok, r.error, r.maybe_delivered) == (False, "HTTP 503", False)
    finally:
        srv.shutdown()
        srv.server_close()


def test_dry_run_sink_emits_and_survives_a_broken_console():
    out = []
    assert DryRunSink(ManualClock(), out.append).send(_reply("多少钱"), 0.0).ok and "dry-run" in out[0]

    def broken(_):
        raise UnicodeEncodeError("gbk", "x", 0, 1, "nope")
    assert DryRunSink(ManualClock(), broken).send(_reply("多少钱"), 0.0).ok
