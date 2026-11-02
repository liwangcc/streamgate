"""Live mode: HTTP ``POST /ingest`` on 127.0.0.1 -> Engine -> sink (AI-Vtuber ``POST /send``).

Locking: ingest and selection hold the lock only for in-memory work; decision records are
queued to a writer thread. The sink call (network, up to its timeout) runs outside the lock,
so a slow or dead AI-Vtuber never blocks ingestion -- unlike a global handle_lock
(BRIEF §3, failure mode 3).
"""

from __future__ import annotations

import json
import queue
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .decisions import DecisionLog, dumps
from .engine import Engine
from .events import Event, Rejected, parse_line, parse_obj
from .sinks import is_loopback

MAX_BODY = 1 << 20   # 1 MiB per request
_JSONL = object()     # marker: body is not a single JSON document, read it line by line
_STOP = object()


class LiveService:
    def __init__(self, cfg, policy: str, sink, clock, decisions_path: str | None = None):
        self.cfg = cfg
        self.sink = sink
        self.clock = clock
        self.lock = threading.Lock()
        self.write_errors = 0
        self._q: queue.Queue | None = None
        if decisions_path:
            self._fh = open(decisions_path, "a", encoding="utf-8")
            self._q = queue.Queue()
            self._writer = threading.Thread(target=self._drain, name="streamgate-decisions", daemon=True)
            self._writer.start()
        self.log = DecisionLog(keep=False, writer=self._write, tail=500)
        self.engine = Engine(policy, cfg, self.log)
        self.policy = policy
        self.seq = 0
        self.stats: Counter = Counter()
        self.started = clock.now()
        self.consecutive_failures = 0
        self.last_ok_at: float | None = None

    def _write(self, rec: dict) -> None:
        if self._q is not None:            # called under the lock: memory only
            self._q.put(rec)

    def _drain(self) -> None:
        while True:
            rec = self._q.get()
            try:
                if rec is _STOP:
                    return
                self._fh.write(dumps(rec) + "\n")
                self._fh.flush()
            except Exception:              # a full disk must not stop the room
                self.write_errors += 1
            finally:
                self._q.task_done()

    def flush(self) -> None:
        """Block until every queued decision record is on disk."""
        if self._q is not None:
            self._q.join()

    # ---------------------------------------------------------------- ingest
    def _admit(self, item_fn) -> bool:
        with self.lock:
            self.seq += 1
            now = self.clock.now()
            item = item_fn(self.seq, now)
            self.engine.admit(item, now)
            return isinstance(item, Event)

    def ingest_obj(self, obj) -> bool:
        return self._admit(lambda seq, now: parse_obj(obj, seq, self.cfg, arrival_ts=now))

    def ingest_body(self, body: bytes) -> dict:
        """Accepts one JSON object, a JSON array of objects, or JSONL."""
        accepted = rejected = 0
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            self._admit(lambda seq, now: Rejected(seq, "malformed", "request body is not UTF-8", ts=now))
            return {"accepted": 0, "rejected": 1}
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = _JSONL
        if parsed is not _JSONL:
            for obj in parsed if isinstance(parsed, list) else [parsed]:
                ok = self.ingest_obj(obj)
                accepted, rejected = accepted + ok, rejected + (not ok)
        else:
            for line in text.splitlines():
                if not line.strip():
                    continue
                ok = self._admit(lambda seq, now, line=line: parse_line(line, seq, self.cfg, arrival_ts=now))
                accepted, rejected = accepted + ok, rejected + (not ok)
        return {"accepted": accepted, "rejected": rejected}

    # ---------------------------------------------------------------- dispatch
    def step(self):
        """Select at most one reply and send it. Called by the ticker (or tests)."""
        with self.lock:
            now = self.clock.now()
            reply = self.engine.select(now)
        if reply is None:
            return None
        result = self.sink.send(reply, now)          # outside the lock
        with self.lock:
            self.engine.complete(reply, result, self.clock.now())
            self.stats["sent_ok" if result.ok else "sent_failed"] += 1
            if result.ok:
                self.consecutive_failures, self.last_ok_at = 0, result.at
            else:
                self.consecutive_failures += 1
        return result

    def _alerts(self, now: float) -> list[str]:
        """Operator-facing warnings. The human host is the fallback when the voice pipeline stalls."""
        alerts = []
        if self.consecutive_failures >= 2:
            alerts.append(f"{self.consecutive_failures} consecutive failed sends to AI-Vtuber: replies may not be "
                          "playing; the host should take over until it recovers")
        quiet = now - (self.last_ok_at if self.last_ok_at is not None else self.started)
        if self.engine.pending() > 0 and quiet > self.cfg.silence_guard_s:
            alerts.append(f"no confirmed send for {quiet:.0f}s while {self.engine.pending()} event(s) are waiting")
        return alerts

    # ---------------------------------------------------------------- views
    def snapshot(self) -> dict:
        with self.lock:
            outcomes = Counter()
            for (outcome, reason), n in self.log.outcomes.items():
                outcomes[f"{outcome}/{reason}"] += n
            return {
                "policy": self.policy, "uptime_s": round(self.clock.now() - self.started, 3),
                "events": self.seq, "pending": self.engine.pending(),
                "replies_ok": self.stats["sent_ok"], "replies_failed": self.stats["sent_failed"],
                "outcomes": dict(sorted(outcomes.items())),
                "internal_errors": self.engine.internal_errors, "last_internal_error": self.engine.last_error,
                "http_errors": self.stats["http_error"],
                "decision_writer_errors": self.log.writer_errors + self.write_errors,
                "llm_calls": self.stats["sent_ok"] + self.stats["sent_failed"],
                "consecutive_send_failures": self.consecutive_failures,
                "last_ok_send_age_s": None if self.last_ok_at is None else round(self.clock.now() - self.last_ok_at, 3),
                "alerts": self._alerts(self.clock.now()),
            }

    def recent(self, limit: int) -> list[dict]:
        with self.lock:
            return list(self.log.tail)[-max(1, min(limit, 500)):]

    def count(self, key: str) -> None:
        with self.lock:
            self.stats[key] += 1

    def close(self) -> None:
        if self._q is not None:
            self._q.put(_STOP)
            self._writer.join()
            self._fh.close()
            self._q = None


def make_server(service: LiveService, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if not is_loopback(host):
        raise ValueError(f"refusing to bind {host!r}: Streamgate has no auth and listens on loopback only")

    class Handler(BaseHTTPRequestHandler):
        server_version = "streamgate/0.1"

        def log_message(self, fmt, *args):   # keep the console for decisions
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, exc: Exception) -> None:
            service.count("http_error")
            try:
                self._json(500, {"error": type(exc).__name__})
            except Exception:
                pass

        def do_GET(self):
            try:
                url = urlparse(self.path)
                if url.path == "/healthz":
                    self._json(200, {"ok": True})
                elif url.path == "/metrics":
                    self._json(200, service.snapshot())
                elif url.path == "/decisions":
                    q = parse_qs(url.query)
                    try:
                        limit = int(q.get("limit", ["50"])[0])
                    except ValueError:
                        limit = 50
                    self._json(200, service.recent(limit))
                else:
                    self._json(404, {"error": "not found"})
            except Exception as e:
                self._fail(e)

        def do_POST(self):
            try:
                if urlparse(self.path).path != "/ingest":
                    self._json(404, {"error": "not found"})
                    return
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self.close_connection = True
                    self._json(411, {"error": "Content-Length required"})
                    return
                if n < 0 or n > MAX_BODY:
                    self.close_connection = True
                    service.count("body_too_large")
                    self._json(413, {"error": f"body larger than {MAX_BODY} bytes"})
                    return
                self._json(200, service.ingest_body(self.rfile.read(n)))
            except Exception as e:
                self._fail(e)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


class Ticker(threading.Thread):
    def __init__(self, service: LiveService, interval_s: float):
        super().__init__(name="streamgate-ticker", daemon=True)
        self.service = service
        self.interval_s = interval_s
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.wait(self.interval_s):
            try:
                self.service.step()
            except Exception:   # the ticker must outlive any single failure
                self.service.count("ticker_error")

    def stop(self) -> None:
        self.stopped.set()
