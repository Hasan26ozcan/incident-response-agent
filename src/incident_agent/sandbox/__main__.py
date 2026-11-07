"""``python -m incident_agent.sandbox`` - inspect the sandbox, run commands in it, run the escape self-test.

    python -m incident_agent.sandbox commands
    python -m incident_agent.sandbox backends
    python -m incident_agent.sandbox run INC-001 "logs-grep timeout --max 5"
    python -m incident_agent.sandbox run INC-001 "restart-service checkout-api" --approve FINGERPRINT --approved-by NAME
    python -m incident_agent.sandbox selftest [--backend docker|namespace|process]

``--approve`` is a local stand-in for the human gate (Stage 18): a demonstration, not a boundary against an agent
that can run this CLI itself.
"""

from __future__ import annotations

import argparse
import sys

from incident_agent.sandbox.approval import issue_approval
from incident_agent.sandbox.audit import AuditLog
from incident_agent.sandbox.backends import all_backends, describe_backends, select_backend
from incident_agent.sandbox.commands import SPECS
from incident_agent.sandbox.runner import SandboxRunner
from incident_agent.sandbox.selftest import format_report, has_failures, run_selftest

DEFAULT_AUDIT = ".cache/sandbox-audit.jsonl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m incident_agent.sandbox", description="Sandboxed command execution")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("commands", help="list the allow-listed commands")
    sub.add_parser("backends", help="show which isolation backends are available")
    run = sub.add_parser("run", help="run one command line in the sandbox")
    run.add_argument("incident")
    run.add_argument("command_line")
    run.add_argument("--backend", choices=sorted(all_backends()))
    run.add_argument("--audit", default=DEFAULT_AUDIT)
    run.add_argument("--approve", metavar="FINGERPRINT", help="approve the command with this fingerprint")
    run.add_argument("--approved-by", metavar="NAME")
    st = sub.add_parser("selftest", help="run the sandbox-escape attempts against a backend")
    st.add_argument("--backend", choices=sorted(all_backends()))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "commands":
        for name in sorted(SPECS):
            spec = SPECS[name]
            print(f"{'HIGH' if spec.mutating else 'low ':<5} {spec.usage()}  - {spec.summary}")
        return 0
    if args.cmd == "backends":
        print("\n".join(describe_backends()))
        return 0
    try:
        backend = select_backend(args.backend)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "selftest":
        checks = run_selftest(backend)
        print(f"backend: {backend.name}")
        print(format_report(checks))
        return 1 if has_failures(checks) else 0
    runner = SandboxRunner(backend, AuditLog(args.audit))
    approval = None
    if args.approve:
        if not args.approved_by:
            print("error: --approve needs --approved-by NAME", file=sys.stderr)
            return 2
        try:
            approval = issue_approval(args.approve, args.approved_by)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    result = runner.run(args.command_line, args.incident, approval)
    print(f"[{result.status}] backend={result.backend or backend.name} {result.message}")
    if result.output:
        print(result.output)
    return 0 if result.ok else (3 if result.status == "needs_approval" else 1)


if __name__ == "__main__":
    sys.exit(main())
