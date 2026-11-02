"""Blind human labelling for BRIEF §4 (100 labelled replies per policy).

``export`` samples up to N sent replies from each decisions file, shuffles them together and writes:
  <sheet>.csv      what the labeller sees: item, username, content, useful, note
                   (no policy, intent, timing or reply id, so the label cannot be biased by them)
  <sheet>.key.csv  item -> policy, reply id, intent, wait, and the sha256 of the decisions file the
                   row was sampled from; used only when scoring
``score`` reads the filled sheet (+ key) and reports the effective reply rate per policy. Given the
current run (``current``), it also checks that the sheet was exported from *that* run: labels for an
older or different run are not evidence for this one.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

SHEET_COLUMNS = ["item", "username", "content", "useful", "note"]
KEY_COLUMNS = ["item", "policy", "reply_id", "intent", "n_users", "wait_s", "source", "decisions_sha256"]
MIN_PER_POLICY = 100                          # BRIEF §4: 100 labelled replies per policy
REQUIRED_POLICIES = ("fifo", "streamgate")
YES = {"1", "y", "yes", "true", "是", "有用"}
NO = {"0", "n", "no", "false", "否", "没用"}


def key_path(sheet: str | Path) -> Path:
    p = Path(sheet)
    return p.with_name(p.name.removesuffix(".csv") + ".key.csv")


def _sent_replies(data: bytes) -> list[dict]:
    replies = []
    for line in data.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("type") == "reply" and r.get("ok"):
            replies.append(r)
    return sorted(replies, key=lambda r: r["reply_id"])


def run_fingerprint(decisions: str | Path) -> dict:
    """{"policy", "sha256", "replies": {reply_id: (username, content)}} of one decisions.jsonl file.

    The sha256 of the file equals the run's ``decisions_sha256`` (records are written one per line)."""
    data = Path(decisions).read_bytes()
    replies = _sent_replies(data)
    return {"policy": replies[0].get("policy") if replies else None,
            "sha256": hashlib.sha256(data).hexdigest(),
            "replies": {r["reply_id"]: (r.get("username"), r.get("content")) for r in replies}}


def fingerprint_records(policy: str, records: list[dict], sha256: str) -> dict:
    """Same as run_fingerprint, for a run held in memory (compare)."""
    return {"policy": policy, "sha256": sha256,
            "replies": {r["reply_id"]: (r.get("username"), r.get("content"))
                        for r in records if r.get("type") == "reply" and r.get("ok")}}


def export(decisions: list[str | Path], out_csv: str | Path, n: int = 100, seed: int = 0) -> Counter:
    items = []
    for path in decisions:
        data = Path(path).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        replies = _sent_replies(data)
        for r in random.Random(seed).sample(replies, min(n, len(replies))):
            items.append((str(path), sha, r))
    random.Random(seed + 1).shuffle(items)          # interleave policies so the sheet is blind

    out = Path(out_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter = Counter()
    with open(out, "w", encoding="utf-8-sig", newline="") as sf, \
            open(key_path(out), "w", encoding="utf-8-sig", newline="") as kf:   # BOM so Excel reads CJK
        sheet, key = csv.DictWriter(sf, fieldnames=SHEET_COLUMNS), csv.DictWriter(kf, fieldnames=KEY_COLUMNS)
        sheet.writeheader()
        key.writeheader()
        for k, (source, sha, r) in enumerate(items, 1):
            item = f"i{k:03d}"
            sheet.writerow({"item": item, "username": r.get("username"), "content": r.get("content"),
                            "useful": "", "note": ""})
            key.writerow({"item": item, "policy": r.get("policy"), "reply_id": r["reply_id"],
                          "intent": r.get("intent"), "n_users": r.get("n_users"),
                          "wait_s": round(r["sent_at"] - r["lead_ts"], 1), "source": source,
                          "decisions_sha256": sha})
            counts[r.get("policy")] += 1
    return counts


def _label(v: str | None) -> bool | None:
    v = (v or "").strip().lower()
    return True if v in YES else False if v in NO else None


def score(sheet: str | Path, key: str | Path | None = None, timely_s: float = 60.0,
          min_per_policy: int = MIN_PER_POLICY, current: dict[str, dict] | None = None) -> dict:
    """``complete`` only if the sheet matches its key, every row is labelled, and both FIFO and
    Streamgate have >= ``min_per_policy`` labelled rows (BRIEF §4: 100 each). With ``current``
    ({policy: fingerprint}) it must also come from exactly that run. ``problems`` says why not."""
    with open(sheet, encoding="utf-8-sig", newline="") as f:
        sheet_rows = {row["item"]: row for row in csv.DictReader(f)}
    labels = {i: _label(row.get("useful")) for i, row in sheet_rows.items()}
    kp = Path(key) if key else key_path(sheet)
    keyrows = {}
    if kp.exists():
        with open(kp, encoding="utf-8-sig", newline="") as f:
            keyrows = {row["item"]: row for row in csv.DictReader(f)}

    def summarize(items: list[str]) -> dict:
        got = [i for i in items if labels.get(i) is not None]
        useful = [i for i in got if labels[i]]
        timely = [i for i in useful if i in keyrows and float(keyrows[i]["wait_s"]) <= timely_s]
        return {"rows": len(items), "labeled": len(got), "unlabeled": len(items) - len(got),
                "useful": len(useful),
                "effective_reply_rate": round(len(useful) / len(got), 4) if got else None,
                "effective_reply_rate_timely": round(len(timely) / len(got), 4) if got and keyrows else None}

    out = {"overall": summarize(list(labels)), "per_policy": {}, "required_per_policy": min_per_policy}
    for pol in sorted({r["policy"] for r in keyrows.values()}):
        out["per_policy"][pol] = summarize([i for i, r in keyrows.items() if r["policy"] == pol and i in labels])
        out["per_policy"][pol]["missing_from_sheet"] = sum(
            1 for i, r in keyrows.items() if r["policy"] == pol and i not in labels)
    pp = out["per_policy"]

    problems = []
    if not keyrows:
        problems.append(f"answer key {kp} not found, so per-policy counts are unknown")
    else:
        missing = sum(1 for i in keyrows if i not in labels)
        extra = sum(1 for i in labels if i not in keyrows)
        if missing:
            problems.append(f"{missing} item(s) in the key are missing from the sheet")
        if extra:
            problems.append(f"{extra} sheet row(s) are not in the key")
    if out["overall"]["unlabeled"]:
        problems.append(f"{out['overall']['unlabeled']} row(s) not labelled")
    for pol in REQUIRED_POLICIES:
        n = pp.get(pol, {}).get("labeled", 0)
        if n < min_per_policy:
            problems.append(f"{pol}: {n} labelled row(s), need >= {min_per_policy}")
    out["binding_checked"] = current is not None
    if current is not None:
        problems += _binding_problems(keyrows, sheet_rows, current)
    out["problems"] = problems
    out["complete"] = not problems
    if out["complete"] and "fifo" in pp and "streamgate" in pp:
        f, s = pp["fifo"]["effective_reply_rate"], pp["streamgate"]["effective_reply_rate"]
        out["brief_s4_pass"] = s is not None and f is not None and s >= 0.70 and s - f >= 0.30
    return out


def _binding_problems(keyrows: dict, sheet_rows: dict, current: dict[str, dict]) -> list[str]:
    """Labels count only for the run they were exported from: same decisions sha256, and every sheet
    row equal to the reply that run actually sent under that reply id."""
    problems = []
    for pol in sorted({r["policy"] for r in keyrows.values()}):
        rows = [(i, r) for i, r in keyrows.items() if r["policy"] == pol]
        cur = current.get(pol)
        if cur is None:
            problems.append(f"{pol}: the current run has no decisions for this policy")
            continue
        shas = {r.get("decisions_sha256") or "" for _, r in rows}
        if shas == {""}:
            problems.append(f"{pol}: answer key has no decisions_sha256 (exported by an older version); "
                            "re-run annotate on the current decisions")
        elif shas != {cur["sha256"]}:
            old = sorted(s for s in shas if s)[0]
            problems.append(f"{pol}: sheet was exported from a different run (decisions sha256 {old[:12]}..., "
                            f"current run {cur['sha256'][:12]}...); re-run annotate and label again")
        mismatched = sum(1 for i, r in rows if i in sheet_rows and cur["replies"].get(r["reply_id"])
                         != (sheet_rows[i].get("username"), sheet_rows[i].get("content")))
        if mismatched:
            problems.append(f"{pol}: {mismatched} sheet row(s) differ from the reply this run actually sent")
    return problems
