# DEVLOG

记录重大设计决策，每条一页 ADR 即可：

```
## YYYY-MM-DD — 决策标题
- Decision:
- Why:
- Evidence:
- Trade-offs:
- Open questions:
```

---

## 2026-11-02 — Milestone 1: replay harness + FIFO baseline (measured before any optimization)

### ADR-1: Stdlib-only Python, one package, no runtime dependencies
- Decision: Python ≥3.10, runtime uses only the standard library (`json`, `http.server`, `urllib`,
  `hashlib`, `random` for the generator only). `pytest` is the only dev dependency.
- Why: BRIEF §5 asks for CPU-only, Windows-first, no separate inference engine for non-technical users.
  Nothing in the problem needs numpy or a web framework.
- Trade-offs: hand-written HTTP server (stdlib `ThreadingHTTPServer`), fine for 127.0.0.1 at ≤100 req/min.

### ADR-2: Replay = discrete-time simulation with an injected clock and a downstream truth model
- Decision: the engine never reads a clock; every call takes `now`. Replay feeds events at their `ts`
  and ticks every 0.1 s of simulated time. A separate `DownstreamSim` models AI-Vtuber's
  LLM (1–3 s) → TTS (0.5–2 s) → one audio channel, with replies lasting the policy's
  estimate ×U(0.8, 1.2). Every draw is a hash of the lead event, not a RNG, so runs are bit-identical
  and two policies answering the same message get the same draw.
- Why: CLAUDE.md requires deterministic tests and no `sleep`. Policies only see their own estimate of
  the channel (in production there is no playback callback from AI-Vtuber), so the simulator also
  measures how wrong that estimate is (`downstream_wait_s`, `max_in_pipeline`).
- Trade-offs: duration and latency distributions are assumptions taken from BRIEF §2, not measurements.

### ADR-3: Metric definitions (what "first response" means)
- Decision: **first response = decision → reply handed to an idle pipeline** =
  `(sent_at − decided_at) + time it then waits in the downstream audio queue`.
  LLM generation, TTS rendering and playback are excluded: in v0.1 the LLM runs inside AI-Vtuber
  after `POST /send` ("发出"), and BRIEF §4 excludes TTS and playback.
  The downstream-wait term stops a policy from gaming the metric by forwarding everything
  at once into AI-Vtuber's own queue (failure mode #4).
- Also reported, so nothing hides behind the definition:
  `viewer_wait_s` (arrival → sent, what a viewer feels), `decision_to_audio_s` (includes LLM+TTS),
  `first_response_incl_unsent_s` (never-sent items counted at the horizon as lower bounds).
- Utilization = audio-busy seconds / [first arrival, max(stream end, last audio end)].
- LLM call ratio: each reply sent to AI-Vtuber triggers one LLM generation there; Streamgate itself
  makes 0 LLM calls. `committed_calls` counts replies the policy committed to (FIFO commits to every
  event on arrival); `ratio_executed` counts only those actually sent before the replay ended.
- Effective reply rate: share of sent replies whose lead message is gold-labelled "should reply";
  the `_timely` variant also requires arrival → sent ≤ 60 s. A seeded sample of 100 mirrors BRIEF §4.

### ADR-4: A synthetic 30-minute replay with separate gold labels
- Decision: `data/replay.sample.jsonl` has 12 events over 8 s; BRIEF §4 is defined on 30 min × ~100/min.
  `streamgate gen-replay` (seed 20261102) writes `data/replay.synth30.jsonl` (2,948 lines, 98/min,
  bursts at min 9–12 "sale" 160/min and min 19–21 "demo" 150/min, 1.2% garbage lines, 0.5% adapter
  re-deliveries, 1% out-of-order timestamps) and `data/replay.synth30.gold.jsonl`.
  The sample's gold (`replay.sample.gold.jsonl`) is my hand labelling of the 12 events.
- Why: rates, percentiles and utilization are meaningless on 12 events.
- Guard against circularity: gold comes from the generator's *template category*, never from the
  rules. Two categories are phrased so the keyword rules miss them on purpose
  (`hard_useful`, `hard_risk`), and `chitchat_q` are low-value questions that bait question detectors.
- Trade-offs / honesty: the generator and the rules were written by the same author, so effective-reply
  numbers on synth30 are an **optimistic proxy**. The acceptance-grade number needs a human to label
  100 sent replies: `streamgate annotate` exports them, `streamgate score-annotations` scores them.

### ADR-5: FIFO baseline definition
- Decision: accept every valid event on arrival (decision time = arrival), voice in arrival order,
  never drop. It gets the *same* channel estimate as Streamgate (pipelined, never overlaps audio), so the
  A/B isolates *what* is answered and *when it is decided*. Input hygiene (malformed/unsupported/duplicate
  event id) is shared by both policies, because fail-open is a hard constraint for any pipeline.

### Evidence — FIFO baseline
`python -m streamgate replay data/replay.sample.jsonl --policy fifo` (12 events):
- first response p50 26.3 s, **p95 56.3 s**; 7/12 voiced before the replay ended. The gift (c008, t=5 s)
  was still queued behind 7 comments at the end (BRIEF failure mode #6).

`python -m streamgate replay data/replay.synth30.jsonl --policy fifo` (2,948 lines):

| metric | FIFO |
|---|---|
| first response p50 / **p95** | 849 s / **1,596 s** |
| replies sent / valid events | 199 / 2,917 (backlog 2,704 never sent) |
| effective reply rate (content / timely ≤60 s) | 0.33 / **0.01** |
| risk messages voiced | 10 |
| channel utilization | 0.86 |
| LLM ratio committed / executed | 0.995 / 0.068 |
| useful questions answered within 60 s | 0.16 % |

Backlog curve (pending at end of each minute) grows linearly:
`66 131 185 241 317 405 469 539 615 747 886 1038 … 2362 2441 2535 2617 2710`.

Rules vs gold on synth30 comments (measured here to know the classifier's error before relying on it):
precision 0.90, recall 0.98, risk recall 0.83 (the `hard_risk` templates are missed, by design).

---

## 2026-11-02 — Milestone 2: Streamgate scheduler, measured on the same replays

### ADR-6: Late-binding selection over merged, expiring intent clusters
- Decision: the scheduler keeps a **pending pool of intent clusters**, not a queue, and decides nothing
  until the voice channel can take a reply. At that instant it picks the best cluster, sends it, and
  answers all of its members together. Per event:
  1. **admit**: risk → `risk` (never voiced, routed to the human host); intents with value 0
     (spam/noise/chitchat) → `low_score`; same viewer + same text within 120 s, or viewer already waiting
     in that cluster → `duplicate`; viewer answered < 60 s ago or already has 2 pending → `quota`;
     otherwise join cluster `intent:product-number` (e.g. `price:3`; free-form questions merge only at
     bigram Jaccard ≥ 0.6). The pool is capped at 300; overflow evicts the lowest priority → `quota`.
  2. **expire**: each member has a TTL by intent (commerce 45 s, demo/product/aftersales 60 s,
     gift 90 s, welcome 30 s) → `expired` with the TTL in the detail.
  3. **select** when the channel estimate is free, ≥ 8 s since the last send, and the airtime bucket
     allows it: `priority = value × (1 + 0.3·log2 viewers) × (1 + 0.5·age/TTL) × topic_cooldown`.
     The value is price/how-to-buy 60, shipping/stock 55, coupon 50, after-sales 45, demo 40, other
     question 35, gift 100 + yuan (capped +100), with +5 for fan/new viewers. Topic cooldown is ×0.35 for
     60 s after the same cluster key was answered. A 1 s gather window lets near-simultaneous asks merge.
  4. **airtime token bucket**: refills at 0.70 s of speech per second (capacity 20 s); a reply needs its
     estimated duration in tokens; gifts may borrow up to 10 s (the brief's "headroom for gifts").
  5. **warmth**: welcomes/greetings are voiced only after ≥ 15 s of estimated silence, at most every 120 s.
- Why: FIFO fails because it *commits* on arrival (p95 27 min) and answers in arrival order regardless
  of value. Committing at send time makes decision → send ≈ 0 by construction. TTLs keep the pool
  bounded, merging multiplies coverage (3.7 viewers per reply), and the bucket pins utilization near 70%
  instead of 100%.
- Trade-offs: deciding late means `first_response` is near zero by construction, so the honest viewer
  metric is `viewer_wait_s` (p95 44 s, bounded by TTLs) and `useful_answered_timely_rate`. Every decision
  record carries a `why` (rank, viewers, wait, priority) to answer "为什么回了别人没回我".

### ADR-7: Rules, not an LLM, for scoring (BRIEF §8: don't force an LLM that hasn't won on data)
- Decision: deterministic keyword rules (`rules.py`), with risk checked first and commerce intents
  before greeting/spam. Streamgate makes **0 LLM calls**.
- Evidence: rules vs gold on synth30 comments: precision 0.90, recall 0.98, risk recall 0.83; 99% of
  *sent* replies are useful because selection favours high-value, high-confidence intents.
  An LLM scorer would add 1–3 s (most of the 3 s budget), be non-deterministic, and could not be
  evaluated here (no key, no real labelled data); it has not "won on data", so it is not used.
- Operators can extend the risk lexicon without code changes: `extra_risk_words` in the config JSON.
- Rules changed once after measurement, to fix bugs that unit tests found: "主播好漂亮" was a greeting
  because "主播好" matched as a substring (now a greeting must consist *only* of greeting pieces), and
  size codes ("XL码") are stock questions. Both changes are domain fixes, not template copies; the
  `hard_*` templates are still missed on purpose.

### ADR-8: Gifts are batched, but a gift batch older than 20 s jumps the queue
- Decision: `gift_max_wait_s = 20`. Before this, small-gift batches lost to big question clusters
  (p50 wait 26 s, p95 57 s, 4 gifts never thanked with a 60 s TTL). Gift TTL is now 90 s, and a
  ≥100-yuan gift outranks any question cluster on value alone.
- Evidence (synth30 sweep of `gift_max_wait_s`, measured before the ADR-7 rule fixes):

  | max wait | gift wait p50/p95/max (s) | gift replies / all | useful answered ≤ 60 s |
  |---|---|---|---|
  | 15 | 14.5 / 26.2 / 27.7 | 50 / 138 | 0.41 |
  | **20** | 15.9 / 30.0 / 38.7 | 46 / 139 | 0.39 |
  | 30 | 21.0 / 38.8 / 44.8 | 36 / 133 | 0.41 |
  | 45 | 23.8 / 49.9 / 58.4 | 32 / 132 | 0.42 |
  | off | 25.9 / 57.4 / 64.1 | 29 / 129 | 0.42 |

  The coverage cost is within noise (0.39–0.42), while the gift wait improves clearly. I picked 20 s
  as a round "thanked within about half a minute" product rule. Final run: all 123 gifts thanked,
  wait p50 19 s / p95 31 s / max 39 s.
- Trade-off: a third of reply *slots* are thank-yous, though they are short (~23% of airtime).

### ADR-9: Failure handling — everything becomes a counted decision, nothing raises
- Input: bad JSON/UTF-8, non-objects, missing/non-string/empty text, bad/NaN/negative ts → `malformed`;
  unknown `kind` → `unsupported`; oversize text → truncated to 200 chars and flagged; bad gift value →
  0 and flagged; missing id/user → synthesized per event; re-delivered id → `duplicate`.
- Engine: any exception inside the policy is caught, counted (`internal_errors`) and recorded as
  `internal_error`; the pipeline continues. Tests assert this count is 0 on every replay.
- Sink: `HttpSink.send` never raises (timeout, refused, HTTP 5xx → `ok=false`). A failed send is
  **not retried** (it may have been delivered, and a retry risks a double answer); its members get
  `sink_error`, and sending pauses 5 s. The channel estimate keeps the reply's airtime, so there is no
  overlap even if it did play.

### ADR-10: Live mode — loopback only, lock never held across the network
- Decision: `ThreadingHTTPServer` on 127.0.0.1 (refuses non-loopback binds; sink URL must be localhost;
  proxies bypassed). `POST /ingest` takes a JSON object, an array, or JSONL (≤ 1 MiB). A ticker thread
  selects under the lock, then calls the sink **outside** the lock, so a hung AI-Vtuber cannot stall
  ingestion (failure mode #3). Arrival time is the server's monotonic clock; client `ts` is ignored.
- Evidence: `test_ingest_accepts_object_array_and_jsonl` asserts the sink is never called with the lock
  held; a manual smoke run posted 11 mixed good/garbage events → HTTP 200, the 2 price questions merged
  into one reply, 3 malformed counted, 0 internal errors.

### ADR-11: Reading of "比 FIFO baseline 高 30% 以上"
- Ambiguous: +30 percentage points or ×1.3. The PASS column uses the stricter one (absolute
  ≥ FIFO + 0.30 and ≥ 0.70); the relative uplift is shown as its own row.

### Evidence — final A/B (`python -m streamgate compare <replay>`)

**synth30 (2,948 lines, 30 min)**

| metric | target | FIFO | Streamgate |
|---|---|---|---|
| p95 first response (decision → idle pipeline) | < 3 s | 1,596 s | **0.60 s** (max 2.9 s) |
| effective reply rate, 100-sample (gold) | ≥ 0.70 and ≥ FIFO + 0.30 | 0.36 | **0.99** |
| … timely (sent ≤ 60 s after the ask) | same | 0.01 | **0.99** |
| channel utilization | 0.60–0.80 | 0.86 | **0.70** |
| LLM call ratio (committed) | ≤ 0.15 | 0.995 | **0.047** |
| every event has one decision with a reason | yes | yes | **yes** |
| deterministic (2 runs; also 2 processes, PYTHONHASHSEED 1 vs 999) | yes | yes | **yes** |
| internal errors (31 bad lines, 14 re-deliveries in input) | 0 | 0 | **0** |
| viewer wait p50 / p95 | – | 844 / 1,596 s | 22 / 44 s |
| useful questions answered within 60 s | – | 0.16 % | 41 % |
| risk messages voiced | 0 | 10 | 2 (both a `hard_risk` phrasing) |
| backlog at end | – | 2,704 | 0 (pool 19–74 throughout) |
| silence while live p50 / p95 / max | – | – / – / 5 s | 3.4 / 11 / 18 s |

Drop reasons (Streamgate): expired 1,300 · low_score 880 · risk 93 · quota 71 · duplicate 62 ·
malformed 29 · unsupported 2; merged 373. Max replies in AI-Vtuber's pipeline at once: 1.
Decisions are ≥ 8 s apart; `min_send_gap_s` (7.985) is measured on `sent_at`, which includes sink-latency jitter.

**synth30 with 5% of sends timing out** (`--sink-fail-rate 0.05`): all rows still PASS
(p95 0.64 s, utilization 0.68, effective 1.0); 5 replies failed → `sink_error`, 0 internal errors.

> **Erratum (M3, after REVIEW.md):** "all rows PASS" above was wrong as a claim of acceptance. The risk
> row had no pass/fail check (`pass=None`), so 2 voiced health-claim messages showed as a blank instead of
> FAIL, and the effective-rate rows were judged on synthetic gold, not human labels. Both are fixed in M3.

**sample (12 events)**: p95 first response 56.3 s → 0.07 s; merges c001+c002 and c011+c012; the gift
is thanked at 12 s (FIFO never reaches it); 保过吗 → `risk`; every event explained.
Three rows FAIL **because of sample size, not behaviour**: FIFO voiced only the first 7 events before
the replay ended, 5 of them useful (0.71), so "+30 points" is arithmetically impossible (Streamgate
scores 1.0, a +40% relative uplift); and 6 replies for 12 events cannot be ≤ 15% LLM calls. Those
criteria are rates over a sustained 100/min stream, which is why synth30 exists.

Tests: `python -m pytest -q` → 226 passed (current count, updated in M5; the suite had 147 tests when
M2 was written, 187 at M3, 211 at M4).

### Open questions
1. **Real data.** Every quality number above comes from a generator I wrote. The acceptance-grade
   effective reply rate needs a real 30-min replay and 100 human labels (`annotate` → `score-annotations`).
2. **No playback feedback** from AI-Vtuber: durations are estimated per intent (±20% error simulated).
   If real replies run longer, utilization drifts up. Fix by calibrating `reply_chars` from measured
   audio, or by getting a "playback finished" signal.
3. **Coverage is capacity-bound**: ~4.6 replies/min at 70% airtime covers 41% of useful questions within
   60 s. More would need shorter answers or broader merges (e.g. one answer per product link).
4. **Silence tail**: the longest gaps (18 s) follow a gift thank-you that borrowed airtime and then a long
   demo answer. Option: after a borrow, prefer the best *affordable* cluster.
5. **Demo requests** ("求演示") need the human host to physically show the product; a voiced "好的我演示一下"
   alone is hollow. They might belong in a host-facing queue next to risk messages.
6. **Gift delivery format**: thanks are sent as `type: comment` because only that `/send` schema is
   verified (docs/FACTS.md §2); AI-Vtuber's gift path was not verified.
7. A host-facing view for risk/demo items, and a text overflow channel, are out of scope for v0.1
   (no automated posting).

---

## 2026-11-02 — Milestone 3: response to REVIEW.md

### ADR-12 (P1-1): A voiced risk message is a hard FAIL; health-effect questions are risk
- Decision:
  1. Rules: medical lexicon + `长高 增高 长个 敏感肌 心脏`, plus a health-effect pattern
     `(吃|喝|服用)了?(能|会|可以|没事|有事|有没有|对身体)` ("吃了能…吗", "小孩吃了没事吧", "喝了会失眠吗").
     A pattern generalizes beyond single words; it was checked against every generated message and
     every non-risk template: 0 false positives (tests also pin "主播吃饭了吗", "好吃吗", "用了能退吗" as non-risk).
  2. `compare`: the risk row passes only if `risk_leaks == 0` **and** `risk_leaks_unconfirmed == 0`
     (risky messages inside failed sends that may still have played). It is no longer informational.
  3. Tests: `risk_leaks == 0` on synth30 (clean and with 5% timeouts); the rule test that asserted
     "吃了能长高吗 is product_q" now asserts risk; `extra_risk_words` is tested with a word outside the lexicon.
  4. README no longer says "never voiced": recognised risk is never voiced, rewordings can slip
     through, operators maintain `extra_risk_words`, the human host stays responsible.
- Trade-off (honest): the synth30 `hard_risk` phrasings were held out *on purpose*; they are now covered,
  so risk recall on synth30 (now 1.00) no longer measures unseen phrasings. Recall on real rewordings is
  **unknown** until real data is labelled.

### ADR-13 (P1-2): Acceptance needs human labels; synthetic gold is a proxy
- Decision: the BRIEF §4 effective-rate row is `PENDING` until a human-labelled sheet is supplied
  (`compare --labels`). The synthetic-gold rows stay, renamed "proxy, not acceptance", and never decide.
  The report ends with a verdict: `PASS` / `FAIL: …` / `PENDING: …` / "not an acceptance run".
- Same procedure for both policies: `annotate` takes several decisions files, samples 100 sent replies
  from each, shuffles them into one **blind** sheet (`item, username, content`), and keeps policy, intent
  and timing in a separate `.key.csv`. `score-annotations` reports per-policy rates, the timely variant
  (useful and sent ≤ 60 s after the ask), and `brief_s4_pass`.
- Prepared: `out/annotate/synth30-blind.csv` (200 rows: 100 Streamgate + 100 FIFO) and its key.
  **Not labelled**: labelling it myself would just reproduce the synthetic gold. It needs a person.
- Limitation: merged replies ("…等6位都在问") and multi-name thanks only come from Streamgate, so the
  content can still hint at the policy. Labels on synth30 replies remove the self-labelling circularity,
  but not the synthetic-traffic caveat; the strongest evidence is a real replay labelled the same way.

### ADR-14 (found while re-measuring): a failed send refunds its airtime; a definite failure frees the channel
- Evidence: with 5% simulated timeouts, three consecutive failures produced a **55.8 s silence**. Each
  failure kept the channel reserved (correct for a timeout: it may still play) but also kept its airtime
  tokens spent, so the budget drained with nothing spoken.
- **Superseded in M4 (ADR-16):** the airtime refund now applies only to definite failures; the
  channel handling below is unchanged.
- Decision: on any failed send, refund the airtime. `SinkResult.maybe_delivered` separates ambiguous
  failures (timeout, reset: keep the channel reserved) from definite ones (connection refused, HTTP
  error reply: restore the channel estimate). FIFO gets the same channel handling, to keep the A/B fair.
  Reply records carry `maybe_delivered`, and `risk_leaks_unconfirmed` counts only failures that may have played.
- Result: max silence 55.8 → 17.4 s at 5% timeouts. At 10% / 20% timeouts every automated check still
  passes (utilization 0.69 / 0.65, p95 first response 1.05 / 0.84 s, 0 risk), but runs of consecutive timeouts can
  still leave ~38 s of silence. That is the price of not risking a double answer on an ambiguous
  timeout; FIFO shows 34.6 s at 20%.

### P2 items
- P2-1 README puts viewer wait next to first response and says first response is ~0 by design.
- P2-2 README no longer references `CLAUDE.md`.
- P2-3 replays below acceptance scale (< 1,000 valid events or < 20 min) get a "SMOKE TEST, not
  acceptance" banner and verdict; the 12-event sample now reads that way instead of showing bare FAILs.
- P2-4 `risk_leaks_unconfirmed` (see ADR-12/14).
- P2-5 README: decisions ≥ 8 s apart, sends ≈ 8 s (latency jitter), far above the 1 s window.
- P2-6 live decision records go to a writer thread; the lock now covers memory only (`LiveService.flush()`
  for tests/shutdown).
- P2-7 `Config` validates every field's type and range in `__post_init__` (also for `with_overrides`);
  the CLI prints `config error: …` and exits 2.

### Evidence — after M3 (`python -m streamgate compare data/replay.synth30.jsonl`)

| metric | target | FIFO | Streamgate |
|---|---|---|---|
| p95 first response | < 3 s | 1,600 s | **0.22 s** |
| viewer wait p50 / p95 | – | 846 / 1,600 s | 21 / 42 s |
| effective reply rate, human labels | ≥ 0.70 and ≥ FIFO + 0.30 | pending | **pending** |
| effective reply rate, synthetic gold (proxy) | – | 0.33 | 1.00 |
| risk messages voiced (+ unconfirmed) | 0 | 10 (+0) | **0 (+0)** |
| channel utilization | 0.60–0.80 | 0.86 | **0.70** |
| LLM call ratio | ≤ 0.15 | 0.995 | **0.047** |
| explained / deterministic / internal errors | yes / yes / 0 | yes / yes / 0 | **yes / yes / 0** |
| useful questions answered within 60 s | – | 0.16 % | 41 % |
| longest silence | – | 5 s | 16 s |

Verdict printed by `compare`: **PENDING: automated checks pass; still needed: effective reply rate,
100 human-labelled replies (BRIEF §4)**. With 5% timeouts: same verdict, Streamgate risk 0 (+0),
FIFO 8 (+2 unconfirmed). FIFO numbers moved slightly from M2 because the rule changes alter its
per-intent reply-length estimates. Tests: 187 passed.

### Open questions (added)
8. Who labels the 200-row blind sheet, and is a real 30-minute replay available to label instead?
9. Risk recall on real rewordings is unmeasured; consider a second line of defence for
   low-confidence `product_q` replies (e.g. send them only when the host has confirmed).

---

## 2026-11-02 — Milestone 4: response to REVIEW.md round 2 (M3 review)

Scope: P1, P2-1, P2-2, P3-1, P3-2. P3-3 (prefer the best *affordable* cluster when the best one does
not fit the airtime budget) was deliberately **not** changed. REVIEW.md, BRIEF.md, CLAUDE.md, the replay
data and gold labels were not modified.

### ADR-15 (P1): missing evidence is PENDING, never PASS
- Problem (reproduced before fixing): `data/replay.synth30.jsonl` copied to `out/m4-repro/nogold/`
  (no `.gold.jsonl`) + a complete 100/100 sheet →

  ```
  | effective reply rate, human labels (n=100 / 100) | >= 0.70 and >= FIFO + 0.30 | 0.0 | 1.0 | PASS |
  | risk messages voiced (gold; failed sends counted as unconfirmed) | 0 | n/a | n/a |  |
  **Acceptance: PASS**
  ```
  `_risk_ok()` returned `None` without gold, and `verdict()` ignored `None` rows as informational.
- Decision: (1) without gold the risk row is `PENDING` with the metric text
  "risk messages voiced (cannot verify: gold labels missing)" and value `unverified`; (2) every BRIEF §4
  row carries `required=True`, and `verdict()` treats a required row whose result is `None` as PENDING
  (defence in depth: "could not be checked" can never read as "passed"); (3) the CLI no-gold note now says
  the verdict cannot be PASS; (4) README: without gold, only latency/channel/fail-open rows mean anything.
- Tests: `test_no_gold_with_complete_human_labels_is_pending_never_pass` (unit),
  `test_required_row_without_a_result_is_pending_not_informational`, and the end-to-end CLI tests in
  `tests/test_acceptance_gate.py` (`test_no_gold_plus_complete_labels_is_pending`,
  `test_only_gold_plus_complete_labels_can_pass`). Test temp dirs now live in `out/.pytest-tmp`
  inside the project (`tests/conftest.py` sets pytest's basetemp), so they don't depend on external scratch paths.

### ADR-16 (P2-1): ambiguous failures keep their airtime charged; utilization is reported confirmed and worst case
- Problem: M3 refunded airtime for every failed send, including timeouts that may still have played.
  The reviewer showed that at 20% timeouts, counting those as played gives (1201.5 + 300.7) / 1858.2 = 0.808,
  above 0.80, while the table said 0.6466 PASS.
- Decision: `StreamgatePolicy.on_result` refunds tokens **only** when `maybe_delivered=False`
  (connection refused / HTTP error reply). Metrics add `channel.utilization_worst_case` =
  (confirmed busy s + `maybe_played_s`) / span, where `maybe_played_s` = Σ `est_s` of failed sends with
  `maybe_delivered` true (or missing). `channel.utilization` stays the confirmed value. The report has
  two required rows, confirmed and worst case, and **both** must be within 0.60–0.80.
- Tests: `test_ambiguous_failure_does_not_refund_airtime`, `test_definite_failure_refunds_airtime`,
  `test_no_refund_unit` (direct `on_result` with `SinkResult(ok=False, maybe_delivered=True)`),
  `test_five_percent_timeouts_stay_in_band_confirmed_and_worst_case`, `test_twenty_percent_timeouts_report`
  (checks `maybe_played_s` against the records, the worst-case formula, worst case ≤ 0.80, that both rows
  are required and judged, and that the verdict is PASS iff all required rows pass).
- Cost (honest): this reverses half of ADR-14, and **the 55.8 s silence at 5% timeouts is back**. Each
  ambiguous timeout now holds both its channel reservation and its airtime, and in the simulator a
  timeout never plays. The two failure modes can't both be avoided without knowing whether a timed-out
  reply played: refunding risks > 0.80 airtime if it did, not refunding risks silence if it didn't.
  The reviewer's choice (stay within budget) is the safer one for a live room. The real fix is a
  playback/ack signal from AI-Vtuber (see unresolved).

### ADR-17 (P2-2): send intervals must exceed AI-Vtuber's 1 s keep-last window
- Decision: `Config.__post_init__` rejects `min_send_interval_s` or `fifo_min_send_interval_s` ≤ 1.0 with
  a `ConfigError` naming the field and the reason. Defaults remain 8.0 and 1.1. Before the fix,
  `Config(min_send_interval_s=0)`, `Config(min_send_interval_s=1.0)` and `Config(fifo_min_send_interval_s=0)` were all accepted.
- Tests: `test_send_intervals_must_exceed_aivtuber_window` (0, 0.0, 0.5, 1, 1.0 × both fields),
  `test_send_intervals_just_above_the_window_are_accepted` (1.1), `test_send_interval_defaults_are_unchanged`,
  `test_send_interval_is_validated_when_loading_a_file`. README's "range-checked" sentence now lists the checks.

### P3-1 and P3-2
- P3-1: the M2 "147 passed" line now gives the current count (211) and the historical counts.
- P3-2: `annotate.score()` is `complete` only if the sheet matches its key (no missing or extra items),
  every row is labelled, and FIFO and Streamgate **each** have ≥ 100 labelled rows. It reports
  `required_per_policy`, per-policy `rows`/`labeled`/`missing_from_sheet`, and a `problems` list.
  Test `test_deleting_one_row_makes_the_sheet_incomplete`: deleting one row of a complete 200-row sheet
  gives `complete=false` with "1 item(s) in the key are missing from the sheet" and "… 99 labelled row(s),
  need >= 100", and `compare --labels` stays PENDING. The 12-event sample's sheet (6/7 rows) is now
  incomplete too.

### Other small changes
- `compare`/`replay` with `--sink-fail-rate` and no `--out` now write to `out/<replay>-fail<rate>/`
  instead of overwriting `out/<replay>/`, whose decisions the blind labelling sheet comes from.
  (Stale M3 folders `out/synth30-faults*` were removed.)
- Clean-run decisions are unchanged by M4: `out/replay.synth30/streamgate/metrics.json`
  `decisions_sha256` starts `ca7bc5a33d17c13e`, the hash the reviewer recorded, so
  `out/annotate/synth30-blind.csv` still matches.

### Commands and real outputs (logs in `out/m4-repro/logs/`)

`python -m pytest -q` → `211 passed in 7.44s` (exit 0).

`python -m streamgate compare data/replay.synth30.jsonl [--sink-fail-rate R]`, Streamgate column:

| row | target | R = 0 | R = 0.05 | R = 0.10 | R = 0.20 |
|---|---|---|---|---|---|
| p95 first response (s) | < 3 | 0.224 PASS | 0.23 PASS | 0.246 PASS | 0.167 PASS |
| viewer wait p95 (s) | – | 42.253 | 42.191 | 42.209 | 42.253 |
| human-label effective rate | ≥ 0.70, ≥ FIFO + 0.30 | PENDING | PENDING | PENDING | PENDING |
| risk messages voiced | 0 | 0 PASS | 0 PASS | 0 PASS | 0 PASS |
| utilization, confirmed | 0.60–0.80 | 0.7023 PASS | 0.6654 PASS | 0.6244 PASS | **0.5777 FAIL** |
| utilization, worst case | 0.60–0.80 | 0.7023 PASS | 0.7034 PASS | 0.7036 PASS | 0.7042 PASS |
| LLM call ratio | ≤ 0.15 | 0.047 PASS | 0.047 PASS | 0.047 PASS | 0.047 PASS |
| explained / deterministic / internal errors | yes / yes / 0 | PASS ×3 | PASS ×3 | PASS ×3 | PASS ×3 |
| replies failed | – | 0 | 7 | 17 | 27 |
| useful questions answered ≤ 60 s | – | 0.4133 | 0.3914 | 0.3712 | 0.3485 |
| longest silence (s) | – | 15.817 | 55.836 | 55.836 | 68.423 |
| **verdict** | | PENDING (human labels) | PENDING (human labels) | PENDING (human labels) | **FAIL: voice channel utilization, confirmed** |

FIFO for reference: worst-case utilization 0.8566 / 0.8537 / 0.8521 / 0.8495; risk voiced 10 / 8 (+2
unconfirmed) / 7 (+3) / 7 (+3).

No-gold + complete labels (`out/m4-repro/logs/nogold-repro.txt`; labels are rule-filled FAKE labels used
only to exercise the gate, `score-annotations` → `complete: true`, 100/100):

```
$ python -m streamgate compare out/m4-repro/nogold/replay.jsonl --out out/m4-repro/nogold/out --labels out/m4-repro/FAKE-labels-complete.csv
| effective reply rate, human labels (n=100 / 100) | >= 0.70 and >= FIFO + 0.30 | 0.0 | 1.0 | PASS |
| risk messages voiced (cannot verify: gold labels missing) | 0 | unverified | unverified | PENDING |
**Acceptance: PENDING: automated checks pass; still needed: risk messages voiced (cannot verify: gold labels missing)**
note: no gold labels found: 'risk messages voiced = 0' cannot be verified, so that row is PENDING and the verdict cannot be PASS; ...
```

Control with gold + the same FAKE labels (`withgold-control.txt`): risk row `10 | 0 | PASS`,
`**Acceptance: PASS**`. That only proves the gate logic; its output folder was deleted so that a
fake-label PASS can't be mistaken for evidence.

### Unresolved
1. **Human labels are still missing.** `out/annotate/synth30-blind.csv` (100 + 100, blind) is unfilled; every
   acceptance verdict above stays PENDING until a person fills it. The FAKE sheet in `out/m4-repro/` is not evidence.
2. **20% timeouts fail** on confirmed utilization (0.5777) and can leave 68 s of silence. I'm reporting it as
   FAIL, not tuning it away. 5% and 10% pass both utilization rows.
3. **Silence vs budget under ambiguous timeouts** (ADR-16 cost): 55.8 s max silence at 5% timeouts. It needs a
   delivery/playback signal from AI-Vtuber, or a decision on how long an ambiguous timeout may hold the channel.
4. `worst_case` uses the policy's `est_s`; the simulator's real durations vary ×0.8–1.2, so the bound is an
   estimate, not a guarantee.
5. `compare --labels` does not check that the sheet was exported from *this* replay's decisions
   (the key's `source` and `reply_id` are not matched against the current run); a sheet from another run
   would be accepted. **(Resolved in M5, ADR-19.)**
6. P3-3 (prefer an affordable cluster when the best one doesn't fit the budget) is not implemented, by instruction.

---

## 2026-11-02 — Milestone 5: response to REVIEW.md round 3 (M4 review)

Scope: P1 (fresh-clone pytest), P2 stale sheets, P2 silence trade-off, plus one finding from the live smoke
test (ADR-21). REVIEW.md, BRIEF.md, CLAUDE.md, replay data and gold labels were not modified. Scheduler
decisions are unchanged: clean-run `decisions_sha256` is still `ca7bc5a33d17c13e` (Streamgate) /
`bed2514f8e9a9b7b` (FIFO), under `PYTHONHASHSEED` 1 and 999.

### ADR-18 (P1): the test setup creates `out/` itself
- Problem (reproduced in a clean copy at `out/m5-fresh/before`, no `out/`): `python -m pytest -q` →
  `195 passed, 16 errors`, all `FileNotFoundError: ...\out\.pytest-tmp`. pytest creates the basetemp itself
  but not its parent, and `out/` is git-ignored, so a fresh clone does not have it.
- Fix: `tests/conftest.py` creates `ROOT/out` before setting the basetemp. Test:
  `tests/test_conftest_fresh_clone.py` (parent created in a directory without `out/`; an explicit `--basetemp` is respected).
- Evidence: fresh copy of the final sources (no `out/`) → `226 passed`, and `out/` exists afterwards
  (`out/m5-repro/logs/pytest-fresh-clone.txt`).
- The reviewer's "1 deprecation warning" does not reproduce here (`python -m pytest -q -rw` on Python
  3.11.4 shows none); probably specific to their Python version, so it is not fixed blind.

### ADR-19 (P2): label sheets are bound to the run they were exported from
- Decision (enforced in code, not only documented): `annotate` stores `decisions_sha256` = sha256 of the
  decisions file each row was sampled from (equal to that run's `decisions_sha256`, tested).
  `compare --labels` builds a fingerprint of the run it just made, and `score()` adds a problem, so the
  human row stays PENDING, when:
  - the key's hash differs from the current run ("sheet was exported from a different run (decisions sha256 X…, current run Y…)");
  - a key has no hash (exported by an older version);
  - any sheet row's `username`/`content` differs from the reply this run sent under that reply id
    (catches edited sheets);
  - the current run has no decisions for a policy in the key.
  `score-annotations --decisions <files>` does the same check; without it the output says `binding_checked: false`
  plus a note. When a sheet is supplied but not usable, the PENDING row says why.
- The existing blind sheet: it was still unlabelled (0/200), so it was re-exported from the current clean
  run. **The labeller's sheet is byte-identical** to the M3/M4 one (same deterministic sample; `cmp` OK);
  only the key gained the hash column. Bound check: only "not labelled" problems remain.
- Tests: `test_decisions_file_hash_is_the_run_digest`, `test_sheet_from_another_run_is_not_accepted`,
  `test_edited_sheet_content_is_not_accepted`, `test_key_without_run_hash_is_not_accepted`,
  `test_score_annotations_can_bind_to_decisions`.
- Rule written in README: any change that alters decisions (code, config, replay) requires running
  `annotate` again and labelling the new sheet.

### ADR-20 (P2): accept the conservative timeout policy, but make the silence visible
- Options from the review: (1) accept and document, (2) a target/alert or a cap on consecutive ambiguous
  timeouts, (3) wait for an AI-Vtuber playback/ack signal.
- Decision: **(1) + the alert half of (2), no cap.** A cap would bring back the M3 over-budget risk the
  reviewer asked to remove (P2-1). During a real outage nothing can be voiced anyway, and the silence is the
  outage's, not the scheduler's. What must not happen is "automated checks pass" being read as "experience OK". So:
  - `Config.silence_guard_s = 30` (product guard, validated > 0). Metrics add `silence_guard_s` and
    `silences_over_guard`. The report row "longest silence while live — product guard, not BRIEF §4"
    is **WARN** above the guard. `verdict()` appends `| WARN: …` to every verdict, and a run with only warnings
    reads "PASS with warnings", never bare "PASS".
  - Live `/metrics` adds `consecutive_send_failures`, `last_ok_send_age_s` and `alerts` (≥ 2 failed sends
    in a row, or no confirmed send for > `silence_guard_s` while events wait), the cue for the human host.
  - README has the silence table for 0/5/10/20 % timeouts.
- Tests: `test_silence_guard_row_and_verdict`, `test_five_percent_timeouts_warn_about_silence`,
  `test_silence_guard_must_be_positive`, `test_live_alerts_when_sends_keep_failing`.
- (3) remains the real fix (unresolved #2).

### ADR-21 (live smoke finding): a connect timeout is a definite failure
- Evidence: live smoke with `--sink http://127.0.0.1:8797/send --sink-timeout 0.5` (nothing listening). On
  Windows the closed localhost port surfaced as `URLError: <urlopen error timed out>`, so it was classified
  `maybe_delivered=True`: Streamgate reserved ~12 s of airtime for a reply that cannot have arrived,
  made only 1 attempt in 11 s, and the 2-failure alert never fired. (Windows retries connecting to a
  refused localhost port for ~2 s, so a short timeout expires first.)
- Decision: `HttpSink` uses `http.client` and separates the phases. A failure **while connecting**
  (refused, timeout, any OSError) → `maybe_delivered=False`, because the request never left. A failure after
  connecting (send/read timeout, reset) → `True`. An HTTP error status → `False`, as before. `http.client`
  uses no proxy, so the "traffic stays local" property holds.
- Evidence after: the same smoke run → `connect failed: TimeoutError: timed out`, `maybe_delivered False`,
  2 attempts (t = 1.6 s, 9.6 s, i.e. only the 8 s send interval), and `alerts: ['2 consecutive failed sends
  to AI-Vtuber: …']`. Tests: per-phase fakes (`test_failure_phase_decides_maybe_delivered`, 9 cases) and real
  loopback sockets: closed port → definite, a listener that never answers → ambiguous, a real HTTP
  server → 200 ok / 503 definite.

### Commands and real outputs (logs in `out/m5-repro/logs/`)

- `python -m pytest -q` → `226 passed in 14.17s`; fresh copy without `out/` → `226 passed in 18.40s`.
- `python -m streamgate compare data/replay.synth30.jsonl [--sink-fail-rate R]` (Streamgate column):

| row | R = 0 | R = 0.05 | R = 0.10 | R = 0.20 |
|---|---|---|---|---|
| p95 first response (s) | 0.224 PASS | 0.23 PASS | 0.246 PASS | 0.167 PASS |
| risk voiced | 0 PASS | 0 PASS | 0 PASS | 0 PASS |
| utilization confirmed / worst case | 0.7023 / 0.7023 PASS | 0.6654 / 0.7034 PASS | 0.6244 / 0.7036 PASS | **0.5777 FAIL** / 0.7042 PASS |
| LLM ratio | 0.047 PASS | 0.047 PASS | 0.047 PASS | 0.047 PASS |
| longest silence (guard 30 s) | 15.817 PASS | **55.836 WARN** (1 gap) | **55.836 WARN** (1 gap) | **68.423 WARN** (2 gaps) |
| verdict | PENDING (human labels) | PENDING (human labels) \| WARN silence | PENDING (human labels) \| WARN silence | **FAIL** confirmed utilization \| WARN silence |

- No gold + complete (FAKE, rule-filled, run-bound) labels: `compare out/m5-repro/nogold/replay.jsonl --labels
  out/m5-repro/FAKE-labels-complete.csv` → risk row `unverified | PENDING`,
  `**Acceptance: PENDING: automated checks pass; still needed: risk messages voiced (cannot verify: gold labels missing)**`.
- Stale sheet: the same labels on the 5% run → PENDING, with "fifo: sheet was exported from a different run
  (decisions sha256 bed2514f8e9a..., current run f3db62967906...)", the same for streamgate (`ca7bc5a33d17...` vs
  `a753aac6e0f2...`), and 7 / 4 rows that differ from what that run sent.
- Control: gold + the same FAKE labels on the clean run → `**Acceptance: PASS**`. That only exercises the
  gate; its output folder was deleted.
- Hash determinism: `PYTHONHASHSEED=1` and `999` → fifo `bed2514f8e9a9b7b`, streamgate `ca7bc5a33d17c13e` (both runs).
- Live smoke (dry-run): `accepted 3, rejected 1`, price questions merged into one reply, risk dropped,
  5 records on disk, `internal_errors 0`, `alerts []`. Refused sink: see ADR-21.

### Unresolved
1. **Human labels**: `out/annotate/synth30-blind.csv` (100 + 100, blind, now run-bound) is unfilled; every
   verdict stays PENDING until a person labels it. Fake sheets in `out/m4-repro/` and `out/m5-repro/` are not evidence.
2. **Silence under ambiguous timeouts** (55.8 s at 5%, 68.4 s at 20%) is accepted and made visible
   (WARN + live alerts), not solved. Solving it needs a playback/ack signal from AI-Vtuber.
3. **20% timeouts** still FAIL on confirmed utilization (0.5777), reported as such.
4. The worst-case utilization uses `est_s` (real durations vary ×0.8–1.2), so it is an estimate.
5. P3-3 (prefer an affordable cluster) still not implemented, as instructed in M4.
6. The reviewer's deprecation warning was not reproducible here (Python 3.11.4).
