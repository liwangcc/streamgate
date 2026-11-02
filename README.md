# Streamgate

Admission control and scheduling for live-room replies.

Streamgate is a sidecar that decides which live comments deserve a reply, when to reply,
which ones to merge, and which ones to drop — under a bounded audio budget.

Status: early development. The problem brief is in [BRIEF.md](BRIEF.md).

## Why

A live room can produce far more comments than a single voice channel can answer.
Replying in arrival order builds an unbounded queue; replying to everything is impossible.
Streamgate treats replies as a scheduling problem: score, select, merge, drop, and pace.

## Scope (v0.1)

- Input: JSONL replay or HTTP `POST /ingest`.
- Output: JSONL decisions and an optional AI-Vtuber `/send` sink.
- No platform reverse engineering, no automated posting, no digital-human rendering.

## License

AGPL-3.0. See [LICENSE](LICENSE).
