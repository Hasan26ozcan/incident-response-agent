# ruff: noqa: E501
"""python -m incident_agent.measure  plan | run | report

plan   cascade --sets dev,lab                  what will run and roughly how many calls (spends nothing)
run    cascade --sets dev,lab [--resume]       real provider calls; keys from your local .env only
report cascade                                 comparison table + docs/MEASUREMENTS.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.llm.client import LLMConfigError, LLMUnavailable, _load_dotenv
from incident_agent.measure import campaign as camp
from incident_agent.measure import report as rep
from incident_agent.measure import runner
from incident_agent.measure import spec as spec_mod

OUT_ROOT = Path("eval/measure")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incident_agent.measure")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run", "report"):
        p = sub.add_parser(name)
        p.add_argument("campaign", choices=sorted(camp.CAMPAIGNS))
        p.add_argument("--out", type=Path, default=None, help="default eval/measure/<campaign>")
        if name != "report":
            p.add_argument(
                "--sets", default="dev,lab", help="comma-separated: dev, lab (the test split is not available here)"
            )
        if name == "run":
            p.add_argument("--resume", action="store_true")
            p.add_argument(
                "--pace", type=float, default=0.0, help="seconds to wait between incidents (free-tier rate limits)"
            )
            p.add_argument("--only", default=None, help="run only this config name")
        if name == "report":
            p.add_argument("--reference", default=None, help="config used as the comparison reference")
            p.add_argument("--markdown", type=Path, default=Path("docs/MEASUREMENTS.md"))
    a = ap.parse_args(argv)
    out = a.out or OUT_ROOT / a.campaign
    if a.cmd == "report":
        if not list(out.glob("*__*.json")):
            print(f"no results in {out}: run the campaign first", file=sys.stderr)
            return 2
        report = rep.build(out, a.reference)
        text = rep.render_markdown(report)
        a.markdown.parent.mkdir(parents=True, exist_ok=True)
        a.markdown.write_text(text, encoding="utf-8")
        print(text)
        print(f"wrote {a.markdown}")
        return 0
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    bad = [s for s in sets if s not in camp.SETS]
    if bad:
        print(
            f"unknown set(s) {bad}: choose from {camp.SETS}. The test split is opened only at milestones, with the harness's own flag.",
            file=sys.stderr,
        )
        return 2
    if a.cmd == "plan":
        print(camp.render_plan(camp.plan(a.campaign, sets)))
        return 0
    _load_dotenv()
    configs = [c for c in camp.CAMPAIGNS[a.campaign] if not a.only or c.name == a.only]
    if not configs:
        print(f"no config named {a.only!r}", file=sys.stderr)
        return 2
    missing = sorted({k for c in configs for k in spec_mod.required_keys(c.model)} - set(__import__("os").environ))
    if missing:
        print(
            f"missing in your environment/.env: {', '.join(missing)} (names only; never paste a key into chat)",
            file=sys.stderr,
        )
        return 2
    for c in configs:
        for s in sets:
            try:
                path = runner.run_config(c, s, out, resume=a.resume, pace=a.pace)
            except (LLMConfigError, LLMUnavailable) as exc:
                print(f"{c.name} on {s}: stopped ({exc}). Re-run with --resume.", file=sys.stderr)
                return 1
            print(f"{c.name} on {s} -> {path}")
    print(f"done. Next: python -m incident_agent.measure report {a.campaign}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
