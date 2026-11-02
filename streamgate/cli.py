"""Command line entry point: python -m streamgate <command> ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, ConfigError
from .decisions import dumps
from .engine import POLICIES
from .metrics import compute, default_gold_path, load_gold, summary_lines
from .replay import read_lines, run_replay
from .sim import SimConfig


def _out_dir(arg: str | None, replay: str, fail_rate: float = 0.0) -> Path:
    """Default: out/<replay name>, or out/<replay name>-fail<rate> for failure drills, so a drill
    never overwrites the clean run (whose decisions the labelling sheet is built from)."""
    if arg:
        return Path(arg)
    stem = Path(replay).name.removesuffix(".jsonl")
    return Path("out") / (stem if not fail_rate else f"{stem}-fail{fail_rate:g}")


def _gold(args) -> dict:
    return load_gold(args.gold if args.gold else default_gold_path(args.replay))


def _write_run(out: Path, res, metrics: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "decisions.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for rec in res.records:
            f.write(dumps(rec) + "\n")
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_replay(args) -> int:
    cfg = Config.load(args.config)
    sim = SimConfig(sink_fail_rate=args.sink_fail_rate)
    res = run_replay(read_lines(args.replay), args.policy, cfg, sim)
    metrics = compute(res, cfg, _gold(args))
    out = _out_dir(args.out, args.replay, args.sink_fail_rate) / args.policy
    _write_run(out, res, metrics)
    print("\n".join(summary_lines(metrics)))
    print(f"wrote {out / 'decisions.jsonl'} and {out / 'metrics.json'}")
    return 0


def cmd_compare(args) -> int:
    from .annotate import fingerprint_records, score
    from .report import acceptance, run_both, scale_note, to_markdown, verdict
    cfg = Config.load(args.config)
    sim = SimConfig(sink_fail_rate=args.sink_fail_rate)
    gold = _gold(args)
    runs = run_both(read_lines(args.replay), cfg, sim, gold)
    out = _out_dir(args.out, args.replay, args.sink_fail_rate)
    for policy, (res, metrics) in runs.items():
        _write_run(out / policy, res, metrics)
    human = None
    if args.labels:   # labels only count for the run they were exported from (this one)
        current = {p: fingerprint_records(p, res.records, res.sha256) for p, (res, _) in runs.items()}
        human = score(args.labels, timely_s=cfg.timely_s, current=current)
    rows = acceptance(runs["fifo"][1], runs["streamgate"][1], human)
    note = scale_note(runs["streamgate"][1])
    title = f"{Path(args.replay).name} (sink fail rate {args.sink_fail_rate})"
    md = to_markdown(rows, title, note)
    (out / "compare.md").write_text(md, encoding="utf-8")
    (out / "compare.json").write_text(json.dumps(
        {"replay": str(args.replay), "gold_labels": len(gold), "human_labels": human, "note": note,
         "verdict": verdict(rows, note), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(md)
    if not gold:
        print("note: no gold labels found: 'risk messages voiced = 0' cannot be verified, so that row is "
              "PENDING and the verdict cannot be PASS; synthetic effective-reply rows are n/a")
    if human is not None and not human["complete"]:
        print("note: human labels not usable yet:")
        for p in human["problems"][:10]:
            print(f"  - {p}")
    print(f"wrote {out / 'compare.md'}, {out / 'compare.json'} and per-policy decisions/metrics")
    return 0


def cmd_serve(args) -> int:
    from .clock import MonotonicClock
    from .server import LiveService, Ticker, make_server
    from .sinks import DryRunSink, HttpSink
    cfg = Config.load(args.config)
    clock = MonotonicClock()
    sink = DryRunSink(clock) if args.sink == "dry-run" else HttpSink(args.sink, clock, args.sink_timeout)
    if args.decisions:
        Path(args.decisions).parent.mkdir(parents=True, exist_ok=True)
    service = LiveService(cfg, args.policy, sink, clock, args.decisions)
    srv = make_server(service, args.host, args.port)
    ticker = Ticker(service, cfg.tick_s)
    ticker.start()
    print(f"streamgate {args.policy} listening on http://{args.host}:{srv.server_port} "
          f"(POST /ingest, GET /metrics, GET /decisions); sink: {args.sink}; Ctrl+C to stop", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        ticker.stop()
        srv.server_close()
        service.close()
    return 0


def cmd_annotate(args) -> int:
    from .annotate import export, key_path
    counts = export(args.decisions, args.out, args.n, args.seed)
    print(f"wrote {sum(counts.values())} replies {dict(counts)} to {args.out} (blind: no policy/timing shown)")
    print(f"answer key: {key_path(args.out)} (do not show it to the labeller)")
    print("fill the 'useful' column with 1/0, then run: python -m streamgate score-annotations " + str(args.out))
    return 0


def cmd_score_annotations(args) -> int:
    from .annotate import run_fingerprint, score
    current = None
    if args.decisions:
        current = {}
        for path in args.decisions:
            fp = run_fingerprint(path)
            current[fp["policy"]] = fp
    result = score(args.csv, args.key, Config().timely_s, current=current)
    if current is None:
        result["note"] = ("not checked against a run: pass --decisions, or use compare --labels, before "
                          "treating these labels as evidence")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_gen_replay(args) -> int:
    from .synth import write
    out, gold, n = write(args.out, seed=args.seed, minutes=args.minutes, rate=args.rate)
    print(f"wrote {n} lines to {out} and gold labels to {gold}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="streamgate", description="Reply admission and scheduling sidecar.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def replay_args(sp):
        sp.add_argument("replay", help="replay JSONL file")
        sp.add_argument("--gold", help="gold labels JSONL (default: <replay>.gold.jsonl if present)")
        sp.add_argument("--out", help="output directory (default: out/<replay name>)")
        sp.add_argument("--config", help="JSON file overriding Config fields")
        sp.add_argument("--sink-fail-rate", type=float, default=0.0,
                        help="simulated share of sends that time out (failure-handling drill)")

    r = sub.add_parser("replay", help="run one policy over a replay and write decisions + metrics")
    replay_args(r)
    r.add_argument("--policy", choices=POLICIES, default="streamgate")
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("compare", help="A/B: FIFO baseline vs Streamgate on the same replay")
    replay_args(c)
    c.add_argument("--labels", help="human-labelled blind sheet from 'annotate' (fills the BRIEF §4 row)")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("serve", help="live mode: POST /ingest on 127.0.0.1, replies to AI-Vtuber /send")
    s.add_argument("--host", default="127.0.0.1", help="loopback only (127.0.0.1, ::1, localhost)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--policy", choices=POLICIES, default="streamgate")
    s.add_argument("--sink", default="dry-run",
                   help="'dry-run' (print) or AI-Vtuber URL, e.g. http://127.0.0.1:<port>/send")
    s.add_argument("--sink-timeout", type=float, default=2.0, help="seconds before a send counts as failed")
    s.add_argument("--decisions", help="append decision records to this JSONL file")
    s.add_argument("--config", help="JSON file overriding Config fields")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("annotate", help="export a blind CSV of sampled sent replies for human labelling")
    a.add_argument("decisions", nargs="+", help="one or more decisions.jsonl (e.g. FIFO and Streamgate)")
    a.add_argument("--out", default="out/annotate.csv")
    a.add_argument("--n", type=int, default=100, help="replies sampled per decisions file")
    a.add_argument("--seed", type=int, default=0)
    a.set_defaults(func=cmd_annotate)

    sa = sub.add_parser("score-annotations", help="effective reply rate per policy from a labelled CSV")
    sa.add_argument("csv")
    sa.add_argument("--key", help="answer key (default: <csv name>.key.csv)")
    sa.add_argument("--decisions", nargs="+",
                    help="decisions.jsonl of the run the labels are for; checks the sheet came from it")
    sa.set_defaults(func=cmd_score_annotations)

    g = sub.add_parser("gen-replay", help="generate the deterministic synthetic replay + gold labels")
    g.add_argument("--out", default="data/replay.synth30.jsonl")
    g.add_argument("--seed", type=int, default=20261102)
    g.add_argument("--minutes", type=float, default=30.0)
    g.add_argument("--rate", type=float, default=100.0, help="average events per minute")
    g.set_defaults(func=cmd_gen_replay)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # never die on a console that cannot print CJK
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
