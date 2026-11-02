# Streamgate

Reply admission and scheduling sidecar for a live room with **one voice**. Viewers ask far faster than
the host (or digital human) can answer. Streamgate decides which comments are worth answering, when to
answer them, which to merge, and which to drop — with a recorded reason for every drop.

- Input: JSONL replay, or HTTP `POST /ingest` on 127.0.0.1 (collection is done by an external adapter).
- Output: one decision record per event, plus replies sent to AI-Vtuber `POST /send`.
- Runtime: Python ≥3.10, standard library only, CPU only, Windows first. No LLM calls of its own.

Documents: `BRIEF.md` (problem and acceptance, authoritative) · `docs/FACTS.md` (evidence) ·
`DEVLOG.md` (design decisions and measurements).

**Acceptance status:** on the 30-minute synthetic replay every automated BRIEF §4 check passes
(p95 first response, utilization confirmed and worst case, LLM ratio, zero risk messages voiced,
explainability, determinism, fail-open). The effective reply rate is **pending 100 human labels per
policy** (see "Human labelling"); the synthetic-gold number is only a proxy. The 12-event sample is a
smoke test, not an acceptance run. Under 5% simulated send timeouts the checks still pass; under 20%
the confirmed utilization drops below 0.60 and `compare` reports FAIL (see DEVLOG M4).

**"Automated checks pass" is not "the experience is fine."** Send timeouts make the room go quiet:

| simulated send timeouts | 0% | 5% | 10% | 20% |
|---|---|---|---|---|
| longest silence while live | 15.8 s | **55.8 s** | **55.8 s** | **68.4 s** |
| `compare` | — | WARN | WARN | WARN + FAIL |

A timed-out send may still play, so Streamgate keeps its airtime charged and the channel reserved
(otherwise utilization could exceed 0.80 if it did play). If it did *not* play, the room is silent until
the reservation runs out. Streamgate cannot tell which without a playback signal from AI-Vtuber. So
silences longer than `silence_guard_s` (30 s) show up as **WARN** in the report and verdict (a product
guard, not a BRIEF §4 criterion), and live `/metrics` raises `alerts` so the human host can take over.

Read the latency numbers together: "first response" (decision → send, p95 ≈ 0.2 s) is near zero
*by design*, because Streamgate decides at the moment it can send. What a viewer feels is the
**viewer wait** (their comment → reply sent): p95 ≈ 42 s on the synthetic replay, bounded by TTLs,
vs ≈ 27 min for FIFO.

## Install

Nothing to install for running: from this folder, `python -m streamgate ...` works with a stock Python.

To get the `streamgate` command and the test runner (pytest is the only dependency, dev-only):

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Run the replay (A/B against the FIFO baseline)

```bat
:: 12-event sample shipped with the brief: a smoke test. The report says so, and its rate rows
:: (effective rate, LLM ratio) cannot pass on 12 events; that is expected, not a failure.
python -m streamgate compare data/replay.sample.jsonl

:: 30-minute, ~100 events/min synthetic replay (see "Data" below)
python -m streamgate compare data/replay.synth30.jsonl

:: same, with 5% of sends to AI-Vtuber timing out (failure-handling drill)
python -m streamgate compare data/replay.synth30.jsonl --sink-fail-rate 0.05 --out out/synth30-faults

:: a single policy
python -m streamgate replay data/replay.synth30.jsonl --policy streamgate
python -m streamgate replay data/replay.synth30.jsonl --policy fifo
```

`compare` prints the acceptance table, ending with a verdict (`PASS`, `FAIL: …`, `PENDING: …`, or
"not an acceptance run" for replays far below BRIEF §4 scale), and writes, under `out/<replay name>/`:

| file | content |
|---|---|
| `compare.md`, `compare.json` | BRIEF §4 acceptance table, FIFO vs Streamgate |
| `<policy>/decisions.jsonl` | one `type=event` record per input line (outcome, reason, detail) and one `type=reply` record per send |
| `<policy>/metrics.json` | all metrics, plus a per-minute timeline (arrivals, replies, busy fraction, backlog) |

Gold labels are picked up automatically from `<replay>.gold.jsonl` next to the replay (or pass `--gold`).
**Without gold labels a replay cannot be accepted:** "0 risk messages voiced" cannot be verified, so
that row is `PENDING` and the verdict can never be `PASS`, even with complete human labels. Only the
latency, channel and fail-open rows are meaningful then.

### Metrics

| metric | meaning |
|---|---|
| `first_response_s` p50/p95 | decision → reply handed to an idle pipeline (send + any wait in the downstream audio queue). LLM generation, TTS and playback are excluded (BRIEF §4; in v0.1 the LLM runs inside AI-Vtuber after `/send`). Near zero by design (late binding) — always read it with `viewer_wait_s`. |
| `viewer_wait_s` | arrival → sent, for every event that got answered (lead or merged) — **what a viewer feels** |
| `selection.selection_rate` | events answered (lead + merged) / valid events |
| `drop_reasons` | `low_score` · `expired` · `duplicate` · `risk` · `quota` · `malformed` · `unsupported` · `sink_error` · `unserved`; merges are counted in `merged` |
| `llm.ratio` | committed reply generations / valid events (each send triggers one LLM call in AI-Vtuber; Streamgate itself makes 0) |
| `channel.utilization` | **confirmed**: audio-busy seconds of replies that certainly played / session span (simulated downstream) |
| `channel.utilization_worst_case` | confirmed + the estimated length (`maybe_played_s`) of failed sends that *may* still have played (timeouts) — the upper bound. `compare` requires **both** to be within 0.60–0.80 |
| `quality.*` | with gold labels: effective reply rate (all sent, 100-sample, and "timely ≤ 60 s"), risk messages voiced (`risk_leaks`; risky messages in *failed* sends, which may still have played, in `risk_leaks_unconfirmed`), share of useful questions answered within 60 s, rules-vs-gold precision/recall |
| `decisions_sha256` | fingerprint of the decision stream; identical input + config ⇒ identical hash |

### Decision record example

```json
{"type": "event", "seq": 2, "id": "c002", "ts": 0.6, "kind": "comment", "user_id": "u_1002", "intent": "price",
 "outcome": "merged", "reason": "merged", "at": 1.05, "reply_id": "r00001", "merged_into": "c001",
 "detail": "answered together with c001"}
```

## Connect to AI-Vtuber (live)

Streamgate sits between your comment adapter and AI-Vtuber. AI-Vtuber is not modified; Streamgate only
calls its `POST /send` (docs/FACTS.md §2).

```
comment adapter ──POST /ingest──► Streamgate (127.0.0.1:8765) ──POST /send──► AI-Vtuber ──► LLM → TTS → audio
```

1. Start in dry-run first (prints what it would send, voices nothing):
   ```bat
   python -m streamgate serve --decisions out/live/decisions.jsonl
   ```
2. Point it at AI-Vtuber's API (use the port from AI-Vtuber's own config):
   ```bat
   python -m streamgate serve --sink http://127.0.0.1:<AI-Vtuber port>/send --decisions out/live/decisions.jsonl
   ```
3. Have the adapter post events (one JSON object, a JSON array, or JSONL; max 1 MiB per request):
   ```json
   {"id": "c001", "user_id": "u_1001", "username": "小林", "text": "这个多少钱",
    "kind": "comment", "user_tier": "regular", "gift_value": 0.0, "platform": "douyin"}
   ```
   `kind` is one of `comment`, `gift`, `follow`, `entrance`. In live mode the arrival time is Streamgate's
   own clock; a client `ts` is ignored.
4. Watch `GET /metrics` (counters), `GET /decisions?limit=50` (recent decisions), `GET /healthz`.
   `/metrics` also has `consecutive_send_failures`, `last_ok_send_age_s` and an `alerts` list: it is
   non-empty after 2 failed sends in a row, or when nothing has been sent successfully for longer than
   `silence_guard_s` while events are waiting. That is the cue for the human host to talk.

Notes:
- The server binds loopback only and refuses anything else (it has no auth); the sink URL must be
  localhost as well, and system proxies are bypassed for it.
- Send decisions are ≥ 8 s apart (actual sends ≈ 8 s, give or take a few ms of network latency), far
  above AI-Vtuber's 1-second keep-last window, so it never swallows a reply.
- A failed or timed-out send is recorded (`sink_error`), not retried (it might have been delivered), and
  sending pauses 5 s. Ingestion never waits on AI-Vtuber. A timeout *after connecting* (it may still
  play) keeps its airtime charged and the channel reserved. A definite failure gives the airtime back
  and frees the channel: no connection at all (refused, or a connect timeout, which is how Windows
  reports a closed localhost port) or an HTTP error reply. Consecutive post-connect timeouts can
  therefore leave long silences if those replies never actually played.
- Messages the risk rules **recognise** (medical/health-effect, legal, guarantee, absolute claims) are
  never voiced; they appear as `reason: risk` in `/decisions` for the human host. The rules are a
  keyword lexicon plus a health-effect pattern ("吃了能…/喝了会…"): new rewordings can slip through,
  so operators should add words via `extra_risk_words` as they see them, and the human host stays
  responsible for what is said. Douyin requires a real person to drive a virtual-human stream
  (docs/FACTS.md §4): Streamgate is a decision aid behind that person, and never posts comments.

## Configuration

All numbers live in `streamgate/config.py` (`Config`). Override any of them with a flat JSON file:

```json
{"target_utilization": 0.65, "gift_max_wait_s": 30, "extra_risk_words": ["长高", "敏感肌"]}
```

```bat
python -m streamgate compare data/replay.synth30.jsonl --config my.json
python -m streamgate serve --config my.json
```

The most useful knobs: `target_utilization` (airtime share, default 0.70), `gift_max_wait_s` (20),
`min_send_interval_s` (8), `user_cooldown_s` (60), `extra_risk_words` (operator risk lexicon).
Intent values, TTLs and keyword lists are in `streamgate/rules.py`. Values are type- and range-checked
at startup (numbers finite and non-negative, sizes/rates positive, fractions ≤ 1, `min_reply_s` ≤
`max_reply_s`, and `min_send_interval_s` / `fifo_min_send_interval_s` **> 1 s** because AI-Vtuber keeps
only the last message per 1-second window); a bad config exits with `config error: …` instead of
failing mid-stream.

## Human labelling (the acceptance-grade effective reply rate)

BRIEF §4 asks for 100 human-labelled replies, and the comparison with FIFO must use the same
procedure. `annotate` samples 100 sent replies from **each** decisions file and shuffles them into
one **blind** sheet: the labeller sees only `item, username, content`, not the policy, intent or
timing (those are in a separate `.key.csv`).

```bat
python -m streamgate annotate out/replay.synth30/streamgate/decisions.jsonl out/replay.synth30/fifo/decisions.jsonl --out out/annotate/synth30-blind.csv
:: give ONLY synth30-blind.csv to the labeller; they fill `useful` with 1/0 (是/否 also accepted)
python -m streamgate score-annotations out/annotate/synth30-blind.csv
python -m streamgate compare data/replay.synth30.jsonl --labels out/annotate/synth30-blind.csv
```

`score-annotations` reports the rate per policy (and the same rate restricted to replies sent ≤ 60 s
after the ask), the per-policy row counts, and a `problems` list. The sheet is `complete` only if it
matches its key, every row is labelled, and FIFO and Streamgate **each** have ≥ 100 labelled rows;
otherwise `compare --labels` keeps the row PENDING.

**Labels belong to one run.** The key records the sha256 of the decisions file each row was sampled
from. `compare --labels` checks that this equals the run it just made and that every sheet row equals
the reply that run sent; otherwise the row stays PENDING ("sheet was exported from a different run …").
So after **any** change that alters decisions (code, config, replay), run `annotate` again and label
the new sheet; old labels cannot be reused. `score-annotations` checks the same thing when given
`--decisions <streamgate decisions.jsonl> <fifo decisions.jsonl>`; without it, the output says it was not checked.

Caveat: merged replies
("…等6位都在问") and multi-name thanks only come from Streamgate, so the content can still hint at
the policy even though the metadata is hidden.

## Data

| file | content |
|---|---|
| `data/replay.sample.jsonl` | 12-event sample from the brief |
| `data/replay.sample.gold.jsonl` | my hand labels for those 12 events |
| `data/replay.synth30.jsonl` | 30 min, 2,948 lines, ~98/min with bursts, 1.2% garbage lines, re-deliveries, out-of-order timestamps |
| `data/replay.synth30.gold.jsonl` | gold labels from the generator's template categories (not from the rules) |

Regenerate the synthetic replay (deterministic): `python -m streamgate gen-replay` (seed 20261102).
Its effective-reply and risk numbers are optimistic proxies (the generator and the rules share an
author, and the `hard_risk` phrasings are no longer held out) — see DEVLOG ADR-4 and ADR-12.

## Tests

```bat
python -m pytest
```

Covers input validation, rules, scheduler (merge, TTL, duplicate, quota, risk, gifts, airtime, cooldowns,
sink failures, fail-open), FIFO, replay determinism, the BRIEF §4 criteria on the 30-minute replay, sinks,
the live HTTP server, the generator and the CLI. Tests use a manual clock; nothing sleeps.
Temporary files go to `out/.pytest-tmp` inside the project; `out/` is created if missing, so
a fresh clone works.

## Layout

```
streamgate/
  events.py     input validation (fail open)       rules.py      intent / risk / value rules
  engine.py     hygiene + fail-open policy wrapper  scheduler.py  Streamgate policy
  baseline.py   FIFO policy                         channel.py    speech cost + channel estimate
  replay.py     simulated-clock replay              sim.py        downstream truth model + sim sink
  metrics.py    metrics from decision records       report.py     A/B acceptance table
  server.py     live HTTP ingest + ticker           sinks.py      AI-Vtuber HTTP sink, dry-run sink
  synth.py      synthetic replay generator          annotate.py   human labelling export/score
  config.py     all tunables                        cli.py        command line
```
