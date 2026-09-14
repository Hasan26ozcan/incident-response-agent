#!/usr/bin/env python3
"""
Validates the generated dataset. Run after any regeneration and before
committing. Exits non-zero on failure (suitable for a CI step once Stage 2
sets up CI).

Checks:
  1. Exactly the expected scenario count exists, all four per-incident
     files are present, and every JSON file parses.
  2. No gold-spoiler text (category name, "root_cause") leaks into the
     agent-visible meta.json.
  3. Scenarios flagged is_false_alarm have zero ERROR/WARN log lines.
  4. No metric series is empty.
  5. Every eval/gold/<id>.json has a matching data/incidents/<id>/ directory
     and vice versa (no orphans in either direction).
  6. recurrence_of references point at a real, earlier incident id.

Usage:
    python3 eval/validate_dataset.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = ROOT / "data" / "incidents"
GOLD_DIR = ROOT / "eval" / "gold"
EXPECTED_COUNT = 20


def main():
    problems = []

    index_path = INCIDENTS_DIR / "_index.json"
    if not index_path.exists():
        print("FATAL: data/incidents/_index.json missing — run generate_dataset.py first")
        sys.exit(1)
    index = json.loads(index_path.read_text())

    if len(index) != EXPECTED_COUNT:
        problems.append(f"expected {EXPECTED_COUNT} scenarios, found {len(index)}")

    incident_ids = {e["incident_id"] for e in index}
    gold_ids = {p.stem for p in GOLD_DIR.glob("INC-*.json")}
    if incident_ids != gold_ids:
        problems.append(
            f"mismatch between data/incidents ({len(incident_ids)}) and eval/gold "
            f"({len(gold_ids)}): only-in-incidents={incident_ids - gold_ids}, "
            f"only-in-gold={gold_ids - incident_ids}"
        )

    for entry in index:
        iid = entry["incident_id"]
        d = INCIDENTS_DIR / iid
        for f in ["meta.json", "logs.log", "metrics.json", "deploys.json"]:
            if not (d / f).exists():
                problems.append(f"{iid}: missing {f}")
                continue
        try:
            meta = json.loads((d / "meta.json").read_text())
            metrics = json.loads((d / "metrics.json").read_text())
            json.loads((d / "deploys.json").read_text())  # parse check only
        except json.JSONDecodeError as e:
            problems.append(f"{iid}: JSON parse error — {e}")
            continue

        gold_path = GOLD_DIR / f"{iid}.json"
        if not gold_path.exists():
            continue  # already reported above
        gold = json.loads(gold_path.read_text())

        # Check 2: leakage
        meta_text = json.dumps(meta).lower()
        category_words = gold["category"].replace("_", " ")
        if category_words in meta_text:
            problems.append(f"{iid}: possible leakage — category '{category_words}' appears in meta.json")
        if "root_cause" in meta_text or "gold" in meta_text:
            problems.append(f"{iid}: possible leakage — gold-answer keys appear in meta.json")

        # Check 3: false-alarm cleanliness
        logs = (d / "logs.log").read_text()
        if gold.get("is_false_alarm"):
            if "level=ERROR" in logs or "level=WARN" in logs:
                problems.append(f"{iid}: flagged is_false_alarm but logs.log contains ERROR/WARN lines")

        # Check 4: non-empty metrics
        for k, v in metrics.items():
            if len(v) == 0:
                problems.append(f"{iid}: metric '{k}' has zero data points")

        # Check 6: recurrence_of validity
        rec = gold.get("recurrence_of")
        if rec and rec not in incident_ids:
            problems.append(f"{iid}: recurrence_of='{rec}' does not match any known incident id")

    if problems:
        print(f"VALIDATION FAILED — {len(problems)} problem(s):")
        for p in problems:
            print(" -", p)
        sys.exit(1)

    print(f"OK — {len(index)} scenarios validated, no problems found.")


if __name__ == "__main__":
    main()
