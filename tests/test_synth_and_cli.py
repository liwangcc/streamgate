import csv
import json
from pathlib import Path

from streamgate.cli import main
from streamgate.synth import generate

DATA = Path(__file__).resolve().parent.parent / "data"


def test_generator_is_deterministic_and_matches_committed_replay():
    a_lines, a_gold = generate(20261102)
    b_lines, _ = generate(20261102)
    assert a_lines == b_lines
    committed = (DATA / "replay.synth30.jsonl").read_bytes()
    assert b"\n".join(a_lines) + b"\n" == committed, "regenerate with: python -m streamgate gen-replay"
    assert 2800 <= len(a_lines) <= 3200
    assert generate(1)[0] != a_lines


def test_gold_covers_every_generated_event():
    lines, gold = generate(20261102)
    ids = {g["id"] for g in gold}
    for raw in lines:
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("id", "").startswith("s"):
            assert obj["id"] in ids


def test_cli_replay_compare_annotate(tmp_path, capsys):
    sample = str(DATA / "replay.sample.jsonl")
    assert main(["replay", sample, "--policy", "fifo", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "fifo" / "decisions.jsonl").exists()
    m = json.loads((tmp_path / "fifo" / "metrics.json").read_text(encoding="utf-8"))
    for key in ("first_response_s", "selection", "drop_reasons", "llm", "channel", "quality"):
        assert key in m

    assert main(["compare", sample, "--out", str(tmp_path / "cmp")]) == 0
    assert "| metric |" in (tmp_path / "cmp" / "compare.md").read_text(encoding="utf-8")

    csv_path = tmp_path / "ann.csv"
    sg = str(tmp_path / "cmp" / "streamgate" / "decisions.jsonl")
    fifo = str(tmp_path / "cmp" / "fifo" / "decisions.jsonl")
    assert main(["annotate", sg, fifo, "--out", str(csv_path)]) == 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["item", "username", "content", "useful", "note"]   # blind: no policy/timing
    with open(tmp_path / "ann.key.csv", encoding="utf-8-sig", newline="") as f:
        key = {r["item"]: r for r in csv.DictReader(f)}
    assert {r["policy"] for r in key.values()} == {"fifo", "streamgate"} and len(key) == len(rows)

    capsys.readouterr()
    assert main(["score-annotations", str(csv_path)]) == 0           # nothing labelled yet
    assert json.loads(capsys.readouterr().out)["complete"] is False

    for r in rows:   # PIPELINE-TEST labels by rule, not human judgement
        r["useful"] = "1" if key[r["item"]]["policy"] == "streamgate" else "否"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert main(["score-annotations", str(csv_path)]) == 0
    scored = json.loads(capsys.readouterr().out)
    # every row is labelled, but the 12-event sample has < 100 sent replies per policy
    assert scored["overall"]["unlabeled"] == 0 and scored["complete"] is False
    assert any(p.startswith("streamgate:") and "need >= 100" in p for p in scored["problems"])
    assert any(p.startswith("fifo:") and "need >= 100" in p for p in scored["problems"])
    assert "brief_s4_pass" not in scored
    assert scored["per_policy"]["streamgate"]["effective_reply_rate"] == 1.0

    assert main(["compare", sample, "--out", str(tmp_path / "cmp2"), "--labels", str(csv_path)]) == 0
    md = (tmp_path / "cmp2" / "compare.md").read_text(encoding="utf-8")
    assert "Acceptance: PASS" not in md


def test_cli_reports_bad_config(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"target_utilization": 1.5}', encoding="utf-8")
    assert main(["replay", str(DATA / "replay.sample.jsonl"), "--config", str(bad), "--out", str(tmp_path)]) == 2
    assert "target_utilization" in capsys.readouterr().err


def test_cli_gen_replay(tmp_path):
    out = tmp_path / "mini.jsonl"
    assert main(["gen-replay", "--out", str(out), "--minutes", "2", "--seed", "3"]) == 0
    assert out.exists() and (tmp_path / "mini.gold.jsonl").exists()
