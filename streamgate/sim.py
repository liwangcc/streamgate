"""Replay ground truth: an AI-Vtuber-like downstream (LLM -> TTS -> one audio channel).

Policies never see these timings; they only see their own ChannelEstimate. All
"randomness" is a hash of the reply's lead event, so runs are reproducible and two
policies answering the same lead get the same LLM/TTS/duration draw.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .decisions import Reply, SinkResult, r3


def unit(*parts) -> float:
    """Deterministic uniform [0, 1) from the given parts (stable across processes)."""
    h = hashlib.blake2b("\x1f".join(map(str, parts)).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**64


def _lerp(lo_hi: tuple[float, float], u: float) -> float:
    lo, hi = lo_hi
    return lo + (hi - lo) * u


@dataclass(frozen=True)
class SimConfig:
    llm_s: tuple[float, float] = (1.0, 3.0)          # BRIEF §2
    tts_s: tuple[float, float] = (0.5, 2.0)          # BRIEF §2
    duration_jitter: tuple[float, float] = (0.8, 1.2)  # real reply length vs the policy's estimate
    sink_latency_s: tuple[float, float] = (0.02, 0.08)
    sink_fail_rate: float = 0.0
    sink_timeout_s: float = 2.0


class DownstreamSim:
    def __init__(self, cfg, sim: SimConfig):
        self.cfg = cfg
        self.sim = sim
        self.channel_free_at = -math.inf

    def deliver(self, reply: Reply, sent_at: float) -> dict:
        key = f"{reply.lead.id}#{reply.lead.seq}"
        llm = _lerp(self.sim.llm_s, unit(key, "llm"))
        tts = _lerp(self.sim.tts_s, unit(key, "tts"))
        ready = sent_at + llm + tts
        start = max(ready, self.channel_free_at)
        dur = reply.est_s * _lerp(self.sim.duration_jitter, unit(key, "dur"))
        dur = min(self.cfg.max_reply_s, max(self.cfg.min_reply_s, dur))
        end = start + dur
        self.channel_free_at = end
        return {
            "llm_s": r3(llm), "tts_s": r3(tts), "audio_ready_at": r3(ready),
            "audio_start": r3(start), "audio_end": r3(end), "audio_s": r3(dur),
            "downstream_wait_s": r3(start - ready),
        }


class SimSink:
    def __init__(self, downstream: DownstreamSim):
        self.downstream = downstream
        self.sim = downstream.sim

    def send(self, reply: Reply, now: float) -> SinkResult:
        key = f"{reply.lead.id}#{reply.lead.seq}"
        if unit(key, "fail") < self.sim.sink_fail_rate:
            return SinkResult(False, now + self.sim.sink_timeout_s, "timeout (simulated)", maybe_delivered=True)
        sent_at = now + _lerp(self.sim.sink_latency_s, unit(key, "lat"))
        return SinkResult(True, sent_at, None, self.downstream.deliver(reply, sent_at))
