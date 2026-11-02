"""Streamgate scheduler: late-binding selection over merged, expiring intent clusters.

Lifecycle of an event:
  admit  -> dropped now (risk / low_score / duplicate / quota)  or  joins a cluster
  tick   -> expired members leave (reason: expired)
         -> when the channel can take a reply and airtime allows, the best cluster is
            decided *and* sent in the same instant; its members are answered together.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter

from .channel import ChannelEstimate, estimate_reply_s
from .decisions import Member, Reply
from .rules import INTENTS, bigrams, jaccard


class Cluster:
    __slots__ = ("key", "intent", "slot", "members", "users", "first_seq", "grams",
                 "gift_sum", "n_bonus", "n_truncated")

    def __init__(self, key: str, intent: str, slot: str | None, first_seq: int, grams=None):
        self.key, self.intent, self.slot, self.first_seq, self.grams = key, intent, slot, first_seq, grams
        self.members: dict[int, Member] = {}   # insertion order = arrival order
        self.users: Counter[str] = Counter()
        self.gift_sum = 0.0
        self.n_bonus = 0
        self.n_truncated = 0

    def add(self, m: Member) -> None:
        self.members[m.ev.seq] = m
        self._account(m, +1)

    def remove(self, seq: int) -> Member | None:
        m = self.members.pop(seq, None)
        if m is not None:
            self._account(m, -1)
        return m

    def _account(self, m: Member, d: int) -> None:
        ev = m.ev
        self.users[ev.user_id] += d
        if self.users[ev.user_id] <= 0:
            del self.users[ev.user_id]
        self.gift_sum += d * ev.gift_value
        self.n_bonus += d * (ev.user_tier in ("fan", "new"))
        self.n_truncated += d * ("truncated" in ev.flags)

    @property
    def oldest_ts(self) -> float:
        return next(iter(self.members.values())).ev.ts

    @property
    def newest_seq(self) -> int:
        return next(reversed(self.members))


class StreamgatePolicy:
    name = "streamgate"

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.chan = ChannelEstimate(cfg)
        self.clusters: dict[str, Cluster] = {}
        self.expiry: list[tuple[float, int, str]] = []    # (expires_at, seq, cluster key), lazy deletion
        self.n_pending = 0
        self.user_pending: Counter[str] = Counter()
        self.last_text: dict[tuple[str, str], float] = {}
        self.user_answered: dict[str, float] = {}
        self.topic_answered: dict[str, float] = {}
        self.tokens = cfg.airtime_burst_s
        self.tokens_at: float | None = None
        self.last_welcome = -math.inf
        self.n_replies = 0
        self.next_prune = 0.0
        self.prev_busy: dict[str, float] = {}   # reply id -> channel estimate before that send

    # ------------------------------------------------------------------ admit
    def admit(self, ev, cls, now: float) -> None:
        c, log = self.cfg, self.log
        spec = INTENTS[cls.intent]
        if cls.intent == "risk":
            log.event(ev, "dropped", "risk", now, cls, detail=f"{cls.risk_kind} risk: route to the human host")
            return
        if spec.value <= 0:
            log.event(ev, "dropped", "low_score", now, cls, detail=f"intent {cls.intent} is never voiced")
            return

        if cls.intent != "gift":
            k = (ev.user_id, cls.dedupe)
            last = self.last_text.get(k)
            self.last_text[k] = now
            if last is not None and now - last < c.dup_window_s:
                log.event(ev, "dropped", "duplicate", now, cls,
                          detail=f"same viewer sent the same text {now - last:.1f}s ago")
                return
            t = self.user_answered.get(ev.user_id)
            if t is not None and now - t < c.user_cooldown_s:
                log.event(ev, "dropped", "quota", now, cls,
                          detail=f"viewer was answered {now - t:.1f}s ago (cooldown {c.user_cooldown_s:.0f}s)")
                return
            if self.user_pending[ev.user_id] >= c.max_user_pending:
                log.event(ev, "dropped", "quota", now, cls,
                          detail=f"viewer already has {c.max_user_pending} questions waiting")
                return

        key = self._key(ev, cls)
        cl = self.clusters.get(key)
        if cl is not None and cls.intent != "gift" and ev.user_id in cl.users:
            log.event(ev, "dropped", "duplicate", now, cls, detail=f"viewer already waiting in {key}")
            return
        if cl is None:
            grams = bigrams(cls.dedupe) if cls.intent == "product_q" else None
            cl = self.clusters[key] = Cluster(key, cls.intent, cls.slot, ev.seq, grams)
        cl.add(Member(ev, cls))
        heapq.heappush(self.expiry, (now + spec.ttl_s, ev.seq, key))
        self.user_pending[ev.user_id] += 1
        self.n_pending += 1
        if self.n_pending > c.max_pending:
            self._evict(now)

    def _key(self, ev, cls) -> str:
        if cls.intent in ("gift", "welcome"):
            return cls.intent
        if cls.intent == "product_q":   # free-form questions merge only when nearly identical
            grams = bigrams(cls.dedupe)
            for key, cl in self.clusters.items():
                if (cl.intent == "product_q" and cl.slot == cls.slot
                        and jaccard(cl.grams, grams) >= self.cfg.product_q_merge_jaccard):
                    return key
            return f"product_q#{ev.seq}"
        return f"{cls.intent}:{cls.slot or '*'}"

    def _forget(self, m: Member) -> None:
        uid = m.ev.user_id
        self.user_pending[uid] -= 1
        if self.user_pending[uid] <= 0:
            del self.user_pending[uid]
        self.n_pending -= 1

    def _evict(self, now: float) -> None:
        key = min(self.clusters, key=lambda k: (self._priority(self.clusters[k], now), -self.clusters[k].first_seq))
        cl = self.clusters[key]
        m = cl.remove(cl.newest_seq)
        self._forget(m)
        if not cl.members:
            del self.clusters[key]
        self.log.event(m.ev, "dropped", "quota", now, m.cls,
                       detail=f"pending pool full ({self.cfg.max_pending}); lowest priority evicted")

    # ------------------------------------------------------------------ tick
    def _priority(self, cl: Cluster, now: float) -> float:
        c = self.cfg
        spec = INTENTS[cl.intent]
        v = spec.value
        if cl.intent == "gift":
            v += min(100.0, cl.gift_sum)   # a >=100-yuan gift outranks any question cluster
        elif cl.n_bonus:
            v += 5.0                       # fans and first-time viewers
        if cl.n_truncated == len(cl.members):
            v -= 10.0                      # wall-of-text comments are usually paste spam
        size = 1.0 + 0.3 * math.log2(max(1, len(cl.users)))
        urgency = 1.0 + 0.5 * min(1.0, (now - cl.oldest_ts) / spec.ttl_s)
        last = self.topic_answered.get(cl.key)
        cool = c.topic_cooldown_factor if last is not None and now - last < c.topic_cooldown_s else 1.0
        return v * size * urgency * cool

    def _expire(self, now: float) -> None:
        while self.expiry and self.expiry[0][0] <= now:
            exp_at, seq, key = heapq.heappop(self.expiry)
            cl = self.clusters.get(key)
            m = cl.remove(seq) if cl is not None else None
            if m is None:
                continue                   # already answered or evicted
            self._forget(m)
            if not cl.members:
                del self.clusters[key]
            ttl = INTENTS[m.cls.intent].ttl_s
            self.log.event(m.ev, "dropped", "expired", exp_at, m.cls,
                           detail=f"not reached within its {ttl:.0f}s TTL; higher-priority replies took the airtime")

    def _refill(self, now: float) -> None:
        if self.tokens_at is not None:
            self.tokens = min(self.cfg.airtime_burst_s,
                              self.tokens + (now - self.tokens_at) * self.cfg.target_utilization)
        self.tokens_at = now

    def _prune(self, now: float) -> None:
        c = self.cfg
        self.last_text = {k: t for k, t in self.last_text.items() if now - t < c.dup_window_s}
        self.user_answered = {k: t for k, t in self.user_answered.items() if now - t < c.user_cooldown_s}
        self.topic_answered = {k: t for k, t in self.topic_answered.items() if now - t < c.topic_cooldown_s}
        self.next_prune = now + 30.0

    def tick(self, now: float) -> Reply | None:
        c = self.cfg
        self._expire(now)
        self._refill(now)
        if now >= self.next_prune:
            self._prune(now)
        if not self.clusters or not self.chan.ready(now, c.min_send_interval_s):
            return None

        best, best_rank, eligible = None, None, 0
        idle = self.chan.idle_for(now)
        for cl in self.clusters.values():
            if now - cl.oldest_ts < c.gather_s:
                continue
            if cl.intent == "welcome":    # warmth only: the room has been silent for a while
                if idle < c.warmth_idle_s or now - self.last_welcome < c.welcome_every_s:
                    continue
                p = INTENTS["welcome"].value
            else:
                p = self._priority(cl, now)
                if p < c.min_priority:
                    continue
            eligible += 1
            gift_due = cl.intent == "gift" and now - cl.oldest_ts >= c.gift_max_wait_s
            rank = (gift_due, p, -cl.oldest_ts, -cl.first_seq)
            if best_rank is None or rank > best_rank:
                best, best_rank = cl, rank
        if best is None:
            return None

        est = estimate_reply_s(c, best.intent, len(best.users))
        need = est - (c.gift_reserve_s if best.intent == "gift" else 0.0)
        if self.tokens < need:
            return None                    # keep headroom; re-decide when airtime refills

        del self.clusters[best.key]
        members = list(best.members.values())
        for m in members:
            self._forget(m)
            if best.intent not in ("gift", "welcome"):   # a thank-you is not an answer
                self.user_answered[m.ev.user_id] = now
        self.topic_answered[best.key] = now
        if best.intent == "welcome":
            self.last_welcome = now
        self.tokens -= est
        self.n_replies += 1
        reply_id = f"r{self.n_replies:05d}"
        self.prev_busy[reply_id] = self.chan.on_send(now, est)
        p = best_rank[1]
        return Reply(
            reply_id=reply_id, policy=self.name, intent=best.intent, slot=best.slot,
            members=members, decided_at=now, est_s=est, priority=p, max_names=c.max_names,
            why=(f"best of {eligible} eligible clusters: {best.intent} asked by {len(best.users)} viewer(s), "
                 f"oldest waited {now - best.oldest_ts:.1f}s, priority {p:.1f}"),
        )

    def on_result(self, reply: Reply, result, now: float) -> None:
        prev = self.prev_busy.pop(reply.reply_id, None)
        if not result.ok:
            # Refund airtime only when nothing can have been spoken (refused / HTTP error reply).
            # A timeout may still play downstream: its airtime stays spent and the channel stays
            # reserved, so the worst case (it did play) still respects the utilization budget.
            if not result.maybe_delivered:
                self.tokens = min(self.cfg.airtime_burst_s, self.tokens + reply.est_s)
            self.chan.on_error(result.at, result.maybe_delivered, prev)

    def finish(self, now: float) -> None:
        for cl in list(self.clusters.values()):
            for m in list(cl.members.values()):
                self.log.event(m.ev, "dropped", "unserved", now, m.cls, detail="replay ended while pending")
        self.clusters.clear()
        self.n_pending = 0

    def pending(self) -> int:
        return self.n_pending
