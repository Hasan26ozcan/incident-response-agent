#!/usr/bin/env python3
"""
Regenerates eval/gold_set.md from eval/gold/*.json and data/incidents/*/meta.json.

Run this after any change to data/generate_dataset.py's SCENARIOS list so the
human-readable table never drifts from the machine-readable gold answers.

Usage:
    python3 eval/generate_gold_set_md.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = ROOT / "eval" / "gold"
INCIDENTS_DIR = ROOT / "data" / "incidents"
OUT_PATH = ROOT / "eval" / "gold_set.md"


def main():
    index = json.loads((INCIDENTS_DIR / "_index.json").read_text())
    rows = [json.loads((GOLD_DIR / f"{e['incident_id']}.json").read_text()) for e in index]

    lines = [
        "# Gold Set — Summary Table",
        "",
        "Human-readable index over `eval/gold/*.json`, generated from the same data "
        "`data/generate_dataset.py` produces — regenerate this file "
        "(`python3 eval/generate_gold_set_md.py`) instead of hand-editing it if the "
        "dataset changes.",
        "",
        "| ID | Title | Category | Difficulty | Services | False Alarm | Recurrence Of | Risk Tier | Stage Use |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for g in rows:
        meta = json.loads((INCIDENTS_DIR / g["incident_id"] / "meta.json").read_text())
        services = ", ".join(meta["services"])
        lines.append(
            f"| {g['incident_id']} | {g['title']} | {g['category']} | {g['difficulty']} | "
            f"{services} | {'yes' if g['is_false_alarm'] else '—'} | "
            f"{g['recurrence_of'] or '—'} | {g['remediation_risk_tier']} | "
            f"{', '.join(g['primary_stage_use'])} |"
        )

    lines += [
        "",
        "## Distractor / debate-test pairs",
        "",
        "| Pair | Shared symptom | Distinguishing signal |",
        "|---|---|---|",
        "| INC-005 ↔ INC-018 | Both present as DB-driven slowness on checkout/cart flows | "
        "INC-005: `db_pool_waiting` saturates (connection-bound). INC-018: `db_cpu_pct` "
        "saturates (compute-bound, N+1 queries) |",
        "| INC-007 ↔ INC-017 | Both present as latency spikes with connection resets | "
        "INC-007: `tcp_retransmit_rate` spikes simultaneously with latency (network). "
        "INC-017: `gc_pause_ms` leads latency by ~1 minute (JVM GC) |",
        "",
        "## Recurrence pair (episodic memory test, Stage 12)",
        "",
        "| First encounter | Recurrence | What should transfer |",
        "|---|---|---|",
        "| INC-002 (checkout-api memory leak) | INC-020 (notifications-service memory leak) | "
        "Same causal pattern (unbounded in-memory cache, no eviction) on a different service — "
        "tests generalization, not exact-match recall |",
        "",
        "## False alarm (Stage 8/20 test)",
        "",
        "INC-019 has no real anomaly — a single transient metric blip with an otherwise clean "
        "log stream. See `eval/rubric.md` §5 for the false-positive-handling hard gate this "
        "exists to test.",
        "",
    ]

    OUT_PATH.write_text("\n".join(lines))
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
