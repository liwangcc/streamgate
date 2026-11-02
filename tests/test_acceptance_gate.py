"""End-to-end acceptance gate (REVIEW M3 P1 and P3-2), through the CLI, in a project-local temp dir.

Labels here are filled BY RULE (Streamgate = 1, FIFO = 0) to exercise the gate. They are pipeline-test
labels, never acceptance evidence.
"""

import csv
import json
import shutil
from pathlib import Path

import pytest

from streamgate.cli import main

DATA = Path(__file__).resolve().parent.parent / "data"
SYNTH = DATA / "replay.synth30.jsonl"


def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture(scope="module")
def complete_sheet(tmp_path_factory):
    """synth30 decisions for both policies + a blind sheet with 100/100 rule-filled labels."""
    root = tmp_path_factory.mktemp("gate")
    assert main(["compare", str(SYNTH), "--out", str(root / "run")]) == 0
    sheet = root / "sheet.csv"
    assert main(["annotate", str(root / "run" / "streamgate" / "decisions.jsonl"),
                 str(root / "run" / "fifo" / "decisions.jsonl"), "--out", str(sheet)]) == 0
    policy = {r["item"]: r["policy"] for r in _read(root / "sheet.key.csv")}
    rows = _read(sheet)
    for r in rows:
        r["useful"] = "1" if policy[r["item"]] == "streamgate" else "0"
    _write(sheet, rows)
    return root, sheet


def _verdict(out_dir: Path) -> str:
    return json.loads((out_dir / "compare.json").read_text(encoding="utf-8"))["verdict"]


def test_sheet_with_100_per_policy_is_complete(complete_sheet, capsys):
    _, sheet = complete_sheet
    capsys.readouterr()
    assert main(["score-annotations", str(sheet)]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["complete"] is True and s["problems"] == []
    assert s["per_policy"]["streamgate"]["labeled"] == 100 and s["per_policy"]["fifo"]["labeled"] == 100


def test_deleting_one_row_makes_the_sheet_incomplete(complete_sheet, capsys):
    root, sheet = complete_sheet
    cut = root / "cut.csv"
    rows = _read(sheet)
    _write(cut, rows[1:])                                            # a labeller "loses" one row
    shutil.copyfile(root / "sheet.key.csv", root / "cut.key.csv")
    capsys.readouterr()
    assert main(["score-annotations", str(cut)]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["overall"]["unlabeled"] == 0                           # every remaining row is labelled...
    assert s["complete"] is False                                   # ...but it is not complete
    assert "1 item(s) in the key are missing from the sheet" in s["problems"]
    assert any("99 labelled row(s), need >= 100" in p for p in s["problems"])
    assert main(["compare", str(SYNTH), "--out", str(root / "cut-run"), "--labels", str(cut)]) == 0
    assert _verdict(root / "cut-run").startswith("PENDING")


def test_no_gold_plus_complete_labels_is_pending(complete_sheet, capsys):
    """REVIEW M3 P1: the exact reproduction -- replay copied to a folder without .gold.jsonl."""
    root, sheet = complete_sheet
    nogold = root / "nogold"
    nogold.mkdir()
    shutil.copyfile(SYNTH, nogold / "replay.jsonl")
    assert not (nogold / "replay.gold.jsonl").exists()
    capsys.readouterr()
    assert main(["compare", str(nogold / "replay.jsonl"), "--out", str(nogold / "out"),
                 "--labels", str(sheet)]) == 0
    printed = capsys.readouterr().out
    v = _verdict(nogold / "out")
    assert v.startswith("PENDING") and "gold labels missing" in v
    assert "Acceptance: PASS" not in printed and "risk messages voiced (cannot verify" in printed


def _human(out_dir: Path) -> dict:
    return json.loads((out_dir / "compare.json").read_text(encoding="utf-8"))["human_labels"]


def test_decisions_file_hash_is_the_run_digest(complete_sheet):
    """The key stores sha256(decisions.jsonl); it must equal the run's decisions_sha256."""
    import hashlib
    root, _ = complete_sheet
    for pol in ("fifo", "streamgate"):
        data = (root / "run" / pol / "decisions.jsonl").read_bytes()
        m = json.loads((root / "run" / pol / "metrics.json").read_text(encoding="utf-8"))
        assert hashlib.sha256(data).hexdigest() == m["decisions_sha256"]
    key = _read(root / "sheet.key.csv")
    assert all(len(r["decisions_sha256"]) == 64 for r in key)


def test_sheet_from_another_run_is_not_accepted(complete_sheet):
    """REVIEW M4 P2: labels exported from the clean run are not evidence for a different run."""
    root, sheet = complete_sheet
    out = root / "stale-run"
    assert main(["compare", str(SYNTH), "--sink-fail-rate", "0.05", "--out", str(out), "--labels", str(sheet)]) == 0
    human = _human(out)
    assert human["complete"] is False and human["binding_checked"] is True
    for pol in ("fifo", "streamgate"):
        assert any(p.startswith(f"{pol}: sheet was exported from a different run") for p in human["problems"])
    v = _verdict(out)
    assert v.startswith("PENDING") and "labels not usable" in v and "different run" in v


def test_edited_sheet_content_is_not_accepted(complete_sheet):
    root, sheet = complete_sheet
    rows = _read(sheet)
    rows[0]["content"] = rows[0]["content"] + "（改）"
    edited = root / "edited.csv"
    _write(edited, rows)
    shutil.copyfile(root / "sheet.key.csv", root / "edited.key.csv")
    out = root / "edited-run"
    assert main(["compare", str(SYNTH), "--out", str(out), "--labels", str(edited)]) == 0
    assert any("1 sheet row(s) differ from the reply this run actually sent" in p for p in _human(out)["problems"])
    assert _verdict(out).startswith("PENDING")


def test_key_without_run_hash_is_not_accepted(complete_sheet):
    """A key exported before M5 has no decisions_sha256: it cannot be bound to a run."""
    root, sheet = complete_sheet
    old = root / "old.csv"
    shutil.copyfile(sheet, old)
    key = _read(root / "sheet.key.csv")
    for r in key:
        del r["decisions_sha256"]
    _write(root / "old.key.csv", key)
    out = root / "old-run"
    assert main(["compare", str(SYNTH), "--out", str(out), "--labels", str(old)]) == 0
    assert any("no decisions_sha256" in p for p in _human(out)["problems"])
    assert _verdict(out).startswith("PENDING")


def test_score_annotations_can_bind_to_decisions(complete_sheet, capsys):
    root, sheet = complete_sheet
    capsys.readouterr()
    assert main(["score-annotations", str(sheet)]) == 0
    unbound = json.loads(capsys.readouterr().out)
    assert unbound["binding_checked"] is False and "not checked against a run" in unbound["note"]
    run = root / "run"
    assert main(["score-annotations", str(sheet), "--decisions", str(run / "streamgate" / "decisions.jsonl"),
                 str(run / "fifo" / "decisions.jsonl")]) == 0
    bound = json.loads(capsys.readouterr().out)
    assert bound["binding_checked"] is True and bound["complete"] is True and bound["problems"] == []
    assert main(["compare", str(SYNTH), "--sink-fail-rate", "0.2", "--out", str(root / "f20")]) == 0
    capsys.readouterr()
    assert main(["score-annotations", str(sheet), "--decisions", str(root / "f20" / "streamgate" / "decisions.jsonl"),
                 str(root / "f20" / "fifo" / "decisions.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_only_gold_plus_complete_labels_can_pass(complete_sheet):
    root, sheet = complete_sheet
    assert main(["compare", str(SYNTH), "--out", str(root / "gold-run"), "--labels", str(sheet)]) == 0
    assert _verdict(root / "gold-run") == "PASS"                    # gold present + complete labels
    assert main(["compare", str(SYNTH), "--out", str(root / "gold-nolabels")]) == 0
    assert _verdict(root / "gold-nolabels").startswith("PENDING")   # gold present, no labels
