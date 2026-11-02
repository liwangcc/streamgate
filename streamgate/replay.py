"""Discrete-time replay: feed a JSONL file through an Engine on a fixed simulated clock."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .decisions import digest
from .engine import Engine
from .events import Event, parse_line
from .sim import DownstreamSim, SimConfig, SimSink


@dataclass
class ReplayResult:
    policy: str
    records: list[dict]
    t0: float
    t_last: float
    horizon: float
    lines: int
    blank_lines: int
    internal_errors: int
    backlog: list[tuple[float, int]] = field(default_factory=list)   # (t, pending) each minute

    @property
    def sha256(self) -> str:
        return digest(self.records)


def read_lines(path: str | Path) -> list[bytes]:
    return Path(path).read_bytes().splitlines()


def run_replay(lines: list[bytes] | list[str], policy: str, cfg, sim: SimConfig | None = None) -> ReplayResult:
    sim = sim or SimConfig()
    items = []
    blank = 0
    last_ts = 0.0
    for lineno, raw in enumerate(lines, 1):
        if not raw.strip():
            blank += 1
            continue
        parsed = parse_line(raw, lineno, cfg)
        if isinstance(parsed, Event):
            last_ts = parsed.ts
            ts = parsed.ts
        else:
            ts = parsed.ts if parsed.ts is not None else last_ts   # bad line arrives where it sat
        items.append((ts, lineno, parsed))
    items.sort(key=lambda x: (x[0], x[1]))   # stable: out-of-order timestamps are re-ordered

    engine = Engine(policy, cfg)
    sink = SimSink(DownstreamSim(cfg, sim))
    if not items:
        return ReplayResult(policy, engine.log.records, 0.0, 0.0, 0.0, len(lines), blank, 0)

    t0, t_last = items[0][0], items[-1][0]
    horizon = t_last + cfg.drain_s
    backlog: list[tuple[float, int]] = []
    state = {"k": 0, "next_sample": t0 + 60.0}

    def run_ticks_until(limit: float) -> None:
        while True:
            now = round(t0 + state["k"] * cfg.tick_s, 6)   # integer tick count: no float drift
            if now > limit:
                return
            if now >= state["next_sample"]:
                backlog.append((round(state["next_sample"], 3), engine.pending()))
                state["next_sample"] += 60.0
            reply = engine.select(now)
            if reply is not None:
                engine.complete(reply, sink.send(reply, now), now)
            state["k"] += 1

    for ts, _, parsed in items:
        run_ticks_until(ts)
        engine.admit(parsed, ts)
    run_ticks_until(horizon)
    engine.finish(horizon)

    return ReplayResult(
        policy=policy, records=engine.log.records, t0=t0, t_last=t_last, horizon=horizon,
        lines=len(lines), blank_lines=blank, internal_errors=engine.internal_errors, backlog=backlog,
    )
