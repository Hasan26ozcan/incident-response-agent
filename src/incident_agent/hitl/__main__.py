"""Command line for the approval gate.

    python -m incident_agent.hitl roster add alice --role oncall      # prints alice's token once
    python -m incident_agent.hitl start INC-002 --requested-by bob    # a durable run that waits for a human
    python -m incident_agent.hitl serve [--once]                      # opens requests, escalates, delivers decisions
    python -m incident_agent.hitl list [--status pending] | show REQUEST
    INCIDENT_APPROVER_TOKEN=... python -m incident_agent.hitl approve REQUEST --by alice --confirm FINGERPRINT8
    INCIDENT_APPROVER_TOKEN=... python -m incident_agent.hitl reject  REQUEST --by alice
    python -m incident_agent.hitl demo

State lives in ``.cache/hitl`` (override with ``--root`` or INCIDENT_HITL_DIR); durable runs in INCIDENT_RUNS_DIR.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from incident_agent.durable.engine import Engine, LockError, WorkflowMismatch
from incident_agent.durable.journal import JournalError
from incident_agent.durable.workflows import Deps, default_root, incident_workflow
from incident_agent.hitl.bridge import Controller, request_id_for
from incident_agent.hitl.gate import ApprovalGate, GateError
from incident_agent.hitl.notify import MultiNotifier, Notifier, OutboxNotifier, SlackNotifier
from incident_agent.hitl.policy import EscalationPolicy, Level, PolicyError, load_policy
from incident_agent.hitl.roster import AuthError, Roster
from incident_agent.hitl.store import RequestState, RequestStore
from incident_agent.reporting.sanitize import one_line
from incident_agent.reporting.slack import ConfigError, WebhookSender
from incident_agent.sandbox import select_backend

APPROVER_ENV = "INCIDENT_APPROVER_TOKEN"


def default_hitl_root() -> Path:
    return Path(os.environ.get("INCIDENT_HITL_DIR", ".cache/hitl"))


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    root = Path(args.root) if args.root else default_hitl_root()
    return root, Path(args.runs) if args.runs else default_root()


def _policy(args: argparse.Namespace, root: Path) -> EscalationPolicy:
    explicit = getattr(args, "policy", None)
    if explicit:
        return load_policy(explicit)
    return load_policy(root / "policy.json" if (root / "policy.json").exists() else None)


def _notifier(args: argparse.Namespace, root: Path) -> Notifier:
    """The local outbox always; a real Slack webhook only when --live-slack asked for it (URL from the environment)."""
    outbox = OutboxNotifier(root / "outbox.jsonl")
    if not getattr(args, "live_slack", False):
        return outbox
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        raise ConfigError("--live-slack needs SLACK_WEBHOOK_URL in the environment (see .env.example)")
    return MultiNotifier([outbox, SlackNotifier(WebhookSender(url))])


def _gate(args: argparse.Namespace, clock: Any = time.time) -> tuple[ApprovalGate, Path, Path]:
    root, runs = _paths(args)
    gate = ApprovalGate(
        RequestStore(root / "requests", clock),
        Roster(root / "roster.json"),
        _policy(args, root),
        _notifier(args, root),
        clock,
    )
    return gate, root, runs


def _engine(args: argparse.Namespace, policy: EscalationPolicy, runs: Path) -> Engine:
    backend = getattr(args, "backend", None)
    deps = Deps(backend_factory=lambda: select_backend(backend), approval_timeout=policy.total_timeout)
    return Engine(incident_workflow(deps), runs, poll_interval=0.2)


def _when(ts: float | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")


def describe(st: RequestState) -> str:
    lines = [
        f"request {st.id}: {st.status.upper()}",
        f"  incident {st.incident_id}, run {st.run_id}, risk {st.tier}",
        f"  action: {one_line(st.command_line, 200)}   (fingerprint {st.fingerprint})",
        f"  why: {one_line(st.reason, 200)}",
        f"  needs {st.required} approval(s); have {len(st.approvals)}: "
        f"{', '.join(a['by'] for a in st.approvals) or '-'}",
    ]
    if st.pending:
        lines.append(
            f"  asking level {st.level + 1}/{len(st.levels)} '{st.level_name}' "
            f"(roles {', '.join(st.levels[st.level]['roles'])})"
            f" until {_when(st.level_deadline)}; silence escalates, it never approves"
        )
    else:
        lines.append(f"  resolved {_when(st.resolved_at)}: {st.resolved_reason}")
    bad = [n for n in st.notifications if not n["ok"]]
    if bad:
        lines.append(f"  {len(bad)} notification attempt(s) failed (last: {bad[-1]['detail']})")
    if st.denied:
        lines.append(f"  {st.denied} refused attempt(s) are in the log")
    return "\n".join(lines)


def cmd_roster(args: argparse.Namespace) -> int:
    gate, _root, _ = _gate(args)
    if args.action == "add":
        token = gate.roster.add(args.name, args.role or [])
        print(f"added {args.name}. Token (shown once, keep it secret, export it as {APPROVER_ENV}):\n{token}")
    elif args.action == "remove":
        print("removed" if gate.roster.remove(args.name) else "no such approver")
    else:
        for name, roles in gate.roster.names().items():
            print(f"{name}  roles: {', '.join(roles)}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    gate, _root, runs = _gate(args)
    engine = _engine(args, gate.policy, runs)
    run_id = args.run_id or f"{args.incident.lower()}-{int(time.time())}"
    payload: dict[str, Any] = {"incident_id": args.incident}
    if args.requested_by:
        payload["requested_by"] = args.requested_by
    engine.start(run_id, payload)
    state = engine.run(run_id, block=False)
    print(f"run {run_id}: {state.status}" + (f", waiting for {state.waiting_for}" if state.waiting_for else ""))
    print(f"next: python -m incident_agent.hitl serve   (request id will be {request_id_for(run_id)})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    gate, _root, runs = _gate(args)
    ctl = Controller(gate, _engine(args, gate.policy, runs))
    try:
        while True:
            report = ctl.step()
            for key, items in report.items():
                for item in items:
                    print(f"{key}: {item}")
            if args.once:
                return 1 if report["errors"] else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:  # pragma: no cover
        return 0


def cmd_list(args: argparse.Namespace) -> int:
    gate, _root, _ = _gate(args)
    shown = 0
    for rid in gate.store.ids():
        st = gate.store.state(rid)
        if args.status and st.status != args.status:
            continue
        print(f"{rid}  {st.status}  level={st.level_name}  {one_line(st.command_line, 60)}")
        shown += 1
    if not shown:
        print("no requests")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    gate, _root, _ = _gate(args)
    print(describe(gate.store.state(args.request)))
    return 0


def _decide(args: argparse.Namespace, approve: bool) -> int:
    token = os.environ.get(APPROVER_ENV, "")
    if not token:
        print(f"set {APPROVER_ENV} to your token first (it is never taken as an argument)", file=sys.stderr)
        return 2
    gate, _root, _ = _gate(args)
    if approve:
        st = gate.approve(args.request, args.by, token, confirm=args.confirm or "", comment=args.comment or "")
    else:
        st = gate.reject(args.request, args.by, token, comment=args.comment or "")
    print(describe(st))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, True)


def cmd_reject(args: argparse.Namespace) -> int:
    return _decide(args, False)


def cmd_demo(args: argparse.Namespace) -> int:
    """Two real runs, short policy: one nobody answers (escalates, expires, dry run), one approved by two people."""
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="hitl-demo-"))
    policy = EscalationPolicy(
        (
            Level("primary", ("oncall",), 1.2),
            Level("secondary", ("lead",), 1.2),
            Level("management", ("manager",), 1.2),
        ),
        {"high": 2},
    )
    roster = Roster(root / "roster.json")
    tokens = {n: roster.add(n, [r]) for n, r in (("alice", "oncall"), ("bob", "lead"), ("carol", "manager"))}
    gate = ApprovalGate(RequestStore(root / "requests"), roster, policy, OutboxNotifier(root / "outbox.jsonl"))
    deps = Deps(
        backend_factory=lambda: select_backend(getattr(args, "backend", None)), approval_timeout=policy.total_timeout
    )
    engine = Engine(incident_workflow(deps), root / "runs", poll_interval=0.1)
    ctl = Controller(gate, engine)
    print(
        f"state directory: {root}\npolicy: 3 levels of 1.2 s (primary -> secondary -> management), 2 approvals needed"
    )

    def pump(until: Any, limit: float = 20.0) -> None:
        end = time.monotonic() + limit
        while time.monotonic() < end and not until():
            ctl.step()
            time.sleep(0.15)

    def show_log(rid: str) -> None:
        for e in gate.store.journal(rid).events():
            extra = {k: v for k, v in e.items() if k in ("to", "outcome", "by", "topic", "why", "reason")}
            print(f"    {e['kind']}{' ' + str(extra) if extra else ''}")

    print("\nA) nobody answers")
    engine.start("demo-silent", {"incident_id": "INC-002"})
    engine.run("demo-silent", block=False)
    rid_a = request_id_for("demo-silent")
    pump(lambda: engine.state("demo-silent").status in ("completed", "failed", "cancelled"))
    show_log(rid_a)
    final_a = engine.state("demo-silent")
    exec_a = final_a.results.get("execute", {})
    print(f"  request: {gate.store.state(rid_a).status}; run: {final_a.status}; execute mode: {exec_a.get('mode')}")

    print("\nB) alice (oncall) and bob (lead) approve")
    engine.start("demo-approved", {"incident_id": "INC-002"})
    engine.run("demo-approved", block=False)
    rid_b = request_id_for("demo-approved")
    ctl.step()
    fp = gate.store.state(rid_b).fingerprint[:8]
    gate.approve(rid_b, "alice", tokens["alice"], confirm=fp, comment="looks right")
    print("  alice approved; one approval is not enough (2 needed)")
    pump(lambda: gate.store.state(rid_b).level >= 1)
    gate.approve(rid_b, "bob", tokens["bob"], confirm=fp)
    pump(lambda: engine.state("demo-approved").status in ("completed", "failed", "cancelled"))
    show_log(rid_b)
    final_b = engine.state("demo-approved")
    exec_b = final_b.results.get("execute", {})
    print(
        f"  request: {gate.store.state(rid_b).status}; run: {final_b.status}; execute mode: {exec_b.get('mode')}, "
        f"status: {exec_b.get('status')}, approved by: {exec_b.get('approved_by')}"
    )
    ok = (
        gate.store.state(rid_a).status == "expired"
        and exec_a.get("mode") == "dry_run"
        and gate.store.state(rid_b).status == "approved"
        and exec_b.get("mode") == "applied"
    )
    print("\nRESULT:", "silence escalated and expired without executing; two approvals applied it" if ok else "FAILED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m incident_agent.hitl", description=__doc__.split("\n")[0])
    p.add_argument("--root", default=os.environ.get("INCIDENT_HITL_DIR"), help="gate state directory")
    p.add_argument("--runs", default=os.environ.get("INCIDENT_RUNS_DIR"), help="durable runs directory")
    p.add_argument("--live-slack", action="store_true", help="also post notifications to SLACK_WEBHOOK_URL")
    p.add_argument("--policy", help="escalation policy JSON (default: <root>/policy.json or the built-in one)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("roster")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("name", nargs="?", default="")
    s.add_argument("--role", action="append")
    s.set_defaults(fn=cmd_roster)
    s = sub.add_parser("start")
    s.add_argument("incident")
    s.add_argument("--run-id")
    s.add_argument("--requested-by")
    s.add_argument("--backend")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("serve")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--backend")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("list")
    s.add_argument("--status")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show")
    s.add_argument("request")
    s.set_defaults(fn=cmd_show)
    for name, fn in (("approve", cmd_approve), ("reject", cmd_reject)):
        s = sub.add_parser(name)
        s.add_argument("request")
        s.add_argument("--by", required=True)
        s.add_argument("--comment")
        if name == "approve":
            s.add_argument("--confirm", help="first 8 characters of the action's fingerprint")
        s.set_defaults(fn=fn)
    s = sub.add_parser("demo")
    s.add_argument("--backend")
    s.set_defaults(fn=cmd_demo)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (GateError, AuthError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (KeyError, ValueError, PolicyError, ConfigError, LockError, JournalError, WorkflowMismatch) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
