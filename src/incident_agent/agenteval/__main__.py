"""``python -m incident_agent.agenteval run|list`` - exit 0 = pass, 1 = regression, 2 = error."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from incident_agent.agenteval import suite

OUT = suite.ROOT / "eval" / "results" / "agentic.json"


def render(report: dict, fail: list[str], notes: list[str]) -> str:
    lines = [f"agentic eval (dev split, {len(report['ids'])} incidents)"]
    if "planner" in report:
        p = report["planner"]
        lines.append(
            f"planner    mutating plans {len(p['mutating'])}/{p['n']}, "
            f"false-alarm mutations {len(p['false_alarm_mutations'])}"
        )
    if "agentic" in report:
        s = report["agentic"]["summary"]
        e = s["efficiency"]
        lines.append(
            f"agentic    runs {s['runs']}  completion {s['task_completion']:.3f}  "
            f"SAFETY VIOLATIONS {s['safety_violations']}  events/run {e['events_per_run']}  "
            f"backend/run {e['backend_calls_per_run']}  known gaps open {s['known_gaps_open']}"
        )
    if "controls" in report:
        lines.append(
            "controls   "
            + ", ".join(
                f"{c['control']}={'caught' if c['flagged'] else 'MISSED'}" for c in report["controls"]["controls"]
            )
        )
    for k in ("diagnosis", "retrieval", "memory"):
        if k in report:
            lines.append(
                f"{k:<10} " + json.dumps({a: b for a, b in report[k].items() if a not in ("n",)}, sort_keys=True)
            )
    lines += [f"NOTE  {n}" for n in notes]
    lines += [f"FAIL  {f}" for f in fail]
    lines.append("RESULT " + ("FAIL" if fail else "PASS"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="incident_agent.agenteval", description="Agentic evaluation harness (Stage 19)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list sections and scenarios")
    run = sub.add_parser("run", help="run the suite on the dev split and gate against the baseline")
    run.add_argument("--only", default=None, help=f"comma-separated sections ({', '.join(suite.SECTIONS)})")
    run.add_argument("--ids", default=None, help="comma-separated dev incident ids")
    run.add_argument("--baseline", default=str(suite.BASELINE))
    run.add_argument("--update-baseline", action="store_true", help="write the baseline from this run (deliberate act)")
    run.add_argument("--no-gate", action="store_true", help="report only; do not compare against the baseline")
    run.add_argument("--json", default=str(OUT), help="where to write the full report")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        from incident_agent.agenteval import scenarios, variants

        print("sections: " + ", ".join(suite.SECTIONS))
        for s in (*scenarios.SCENARIOS, scenarios.READ_ONLY):
            print(f"scenario  {s.name:<22} {s.doc}" + ("  [KNOWN GAP]" if s.known_gap else ""))
        for c in variants.CONTROLS:
            print(f"control   {c.name:<22} must be flagged by the {c.catches} metric")
        return 0

    sections = tuple(x.strip() for x in args.only.split(",") if x.strip()) if args.only else suite.SECTIONS
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    fail: list[str]
    notes: list[str]
    try:
        report = suite.run_suite(sections, ids)
        if args.update_baseline:
            if set(sections) != set(suite.SECTIONS) or ids is not None:
                raise suite.SuiteError("--update-baseline needs the full suite on the full dev split")
            fail, _ = suite.gate(report, {**suite.baseline_from(report), "schema": suite.SCHEMA})
            hard = [f for f in fail if f.startswith(("SAFETY", "negative control", "planner proposes", "scenario "))]
            if hard:
                raise suite.SuiteError("refusing to baseline a failing system: " + "; ".join(hard))
            Path(args.baseline).parent.mkdir(parents=True, exist_ok=True)
            Path(args.baseline).write_text(json.dumps(suite.baseline_from(report), indent=2, sort_keys=True) + "\n")
            fail, notes = [], []
            print(f"baseline written to {args.baseline}")
        elif args.no_gate:
            fail, notes = [], []
        else:
            fail, notes = suite.gate(report, suite.load_baseline(Path(args.baseline)))
    except (suite.SuiteError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**report, "failures": fail}, indent=2, sort_keys=True, default=str))
    print(render(report, fail, notes))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover
