"""``python -m incident_agent.security attacks|report|scan|killswitch`` (exit 0 ok, 1 findings, 2 usage/error)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from incident_agent.security import injection
from incident_agent.security.attacks import run_attacks, summarise
from incident_agent.security.killswitch import KillSwitch, KillSwitchError
from incident_agent.security.report import to_markdown

DIR_ENV = "INCIDENT_KILLSWITCH_DIR"
OPERATOR_ENV = "INCIDENT_OPERATOR_SECRET"  # noqa: S105 - the name of the variable, not a secret


def _switch(args: argparse.Namespace) -> KillSwitch:
    return KillSwitch(Path(args.dir or os.environ.get(DIR_ENV) or ".cache/killswitch"))


def cmd_attacks(args: argparse.Namespace) -> int:
    results = run_attacks(only=args.only.split(",") if args.only else None)
    if args.json:
        print(json.dumps({"summary": summarise(results), "results": [r.__dict__ for r in results]}, indent=2))
    else:
        for r in results:
            print(f"{r.id:<7}{r.verdict:<11}{r.title}\n{'':<18}{r.detail}")
        print(json.dumps(summarise(results)["verdicts"]))
    return 1 if summarise(results)["vulnerable"] else 0


def cmd_report(args: argparse.Namespace) -> int:
    results = run_attacks()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(to_markdown(results), encoding="utf-8")
    print(f"wrote {args.out}: {json.dumps(summarise(results)['verdicts'])}")
    return 1 if summarise(results)["vulnerable"] else 0


def cmd_scan(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8", errors="replace")
    findings = injection.scan(text)
    for f in findings:
        print(f"line {f.line}: {f.kind}: {f.excerpt}")
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


def cmd_killswitch(args: argparse.Namespace) -> int:
    ks = _switch(args)
    try:
        if args.action == "status":
            st = ks.state()
            print(f"engaged={st.engaged} source={st.source} by={st.by or '-'} reason={st.reason or '-'}")
            return 1 if st.engaged else 0
        if args.action == "engage":
            ks.engage(args.reason or "no reason given", args.by)
            print("kill switch ENGAGED: no run will start a step and no sandbox command will execute")
            return 0
        token = os.environ.get(OPERATOR_ENV, "")
        if not token:
            raise KillSwitchError(f"export {OPERATOR_ENV} first (it is never accepted as an argument)")
        if args.action == "init-operator":
            ks.set_operator(token)
            print("operator configured (only its SHA-256 digest is stored)")
        else:
            ks.release(args.by, token)
            print("kill switch released")
    except KillSwitchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="incident_agent.security", description="Adversarial testing and security (Stage 20)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("attacks", help="run the attack suite (exit 1 on any FAIL)")
    a.add_argument("--only")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_attacks)
    r = sub.add_parser("report", help="write the Markdown security test report")
    r.add_argument("--out", default="docs/SECURITY_REPORT.md")
    r.set_defaults(fn=cmd_report)
    s = sub.add_parser("scan", help="scan a file (or - for stdin) for injection patterns")
    s.add_argument("file")
    s.set_defaults(fn=cmd_scan)
    k = sub.add_parser("killswitch", help="status | engage | release | init-operator")
    k.add_argument("action", choices=("status", "engage", "release", "init-operator"))
    k.add_argument("--dir", help=f"state directory (default ${DIR_ENV} or .cache/killswitch)")
    k.add_argument("--by", default=os.environ.get("USERNAME") or os.environ.get("USER") or "operator")
    k.add_argument("--reason")
    k.set_defaults(fn=cmd_killswitch)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover
