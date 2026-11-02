"""Live sinks. A sink never raises: every failure becomes ``SinkResult(ok=False)``."""

from __future__ import annotations

import http.client
import ipaddress
import json
from typing import Callable
from urllib.parse import urlparse

from .decisions import Reply, SinkResult


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def aivtuber_payload(reply: Reply) -> dict:
    """Body for AI-Vtuber ``POST /send`` (docs/FACTS.md §2, app/api.py:34-46)."""
    return {"type": "comment",
            "data": {"username": reply.username, "content": reply.content, "platform": reply.lead.platform}}


class DryRunSink:
    """Prints what would be sent. Default for ``serve`` so nothing is voiced by accident."""

    def __init__(self, clock, emit: Callable[[str], None] | None = None):
        self.clock = clock
        self.emit = emit or (lambda line: print(line, flush=True))

    def send(self, reply: Reply, now: float) -> SinkResult:
        try:
            self.emit(f"[dry-run] {reply.reply_id} {reply.intent} -> {json.dumps(aivtuber_payload(reply), ensure_ascii=False)}")
        except Exception:
            pass
        return SinkResult(True, self.clock.now())


class HttpSink:
    """POSTs replies to AI-Vtuber. Loopback only; http.client uses no proxy, so traffic stays local.

    Connecting and sending are separate steps so a failure says whether AI-Vtuber may have the reply:
    - no connection (refused, or a connect timeout -- how Windows reports a closed localhost port
      under a short timeout): the request never left, ``maybe_delivered=False``;
    - an HTTP error status: AI-Vtuber answered and did not take it, ``maybe_delivered=False``;
    - anything after connecting (send/read timeout, reset): it may still play, ``maybe_delivered=True``.
    """

    def __init__(self, url: str, clock, timeout_s: float = 2.0, connection_factory=None):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not is_loopback(parsed.hostname):
            raise ValueError(f"sink URL must be http(s) on localhost/127.0.0.1, got {url!r}")
        self.url = url
        self.clock = clock
        self.timeout_s = timeout_s
        self.path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        host, port = parsed.hostname, parsed.port
        self._connection = connection_factory or (lambda: conn_cls(host, port, timeout=timeout_s))

    def send(self, reply: Reply, now: float) -> SinkResult:
        body = json.dumps(aivtuber_payload(reply), ensure_ascii=False).encode("utf-8")
        conn = None
        try:
            conn = self._connection()
            conn.connect()
        except Exception as e:      # never connected: AI-Vtuber cannot have received it
            _close(conn)
            return SinkResult(False, self.clock.now(), f"connect failed: {type(e).__name__}: {e}"[:200],
                              maybe_delivered=False)
        try:
            conn.request("POST", self.path, body=body,
                         headers={"Content-Type": "application/json; charset=utf-8"})
            resp = conn.getresponse()
            status = resp.status
            resp.read(65536)
        except Exception as e:      # the request may have reached AI-Vtuber: it may still play
            return SinkResult(False, self.clock.now(), f"{type(e).__name__}: {e}"[:200], maybe_delivered=True)
        finally:
            _close(conn)
        if 200 <= status < 300:
            return SinkResult(True, self.clock.now())
        return SinkResult(False, self.clock.now(), f"HTTP {status}", maybe_delivered=False)


def _close(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass
