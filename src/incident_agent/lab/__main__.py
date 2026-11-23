# ruff: noqa: E501
"""python -m incident_agent.lab  list | run LAB-001 [--baseline 20 --fault 40] | run-all | eval --agent llm_react"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.lab import runner
from incident_agent.lab.scenarios import SCENARIOS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incident_agent.lab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("run", "run-all"):
        r = sub.add_parser(name)
        if name == "run":
            r.add_argument("scenario", choices=sorted(SCENARIOS))
        r.add_argument("--baseline", type=float, default=20.0)
        r.add_argument("--fault", type=float, default=40.0)
        r.add_argument("--out", type=Path, default=runner.LAB_DATA)
    e = sub.add_parser("eval", help="score an agent on the captured lab incidents")
    e.add_argument("--agent", default="react_baseline")
    e.add_argument("--ids", default=None, help="comma-separated LAB ids")
    a = ap.parse_args(argv)
    if a.cmd == "eval":
        from incident_agent.lab import evaluate as ev

        rep = ev.evaluate_lab(a.agent, a.ids.split(",") if a.ids else None)
        for row in rep["incidents"]:
            print(
                f"{row['incident_id']}  f1={row['root_cause_f1']:.2f} evidence={row['evidence_recall']:.2f} risk_ok={row['risk_match']} fa_ok={row['false_alarm_ok']} err={row['error'][:60]}"
            )
        print(rep["summary"])
        print("saved", ev.save(rep))
        return 0
    if a.cmd == "list":
        for s in SCENARIOS.values():
            print(f"{s.id}  {s.category:22s} {s.title}")
        return 0
    ids = [a.scenario] if a.cmd == "run" else sorted(SCENARIOS)
    for sid in ids:
        d = runner.run_local(sid, out=a.out, baseline_s=a.baseline, fault_s=a.fault)
        print(f"captured {sid} -> {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
