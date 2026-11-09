"""Command line for durable incident runs.

    python -m incident_agent.durable start INC-001 [--run-id R] [--no-wait] [--crash-after EVENT:STEP] [--backend B]
    python -m incident_agent.durable resume R [--no-wait] [--retry-failed] [--backend B]
    python -m incident_agent.durable signal R approval --by NAME [--reject]
    python -m incident_agent.durable status R | cancel R | list | demo

Runs live in ``.cache/runs/<run_id>/`` (override with INCIDENT_RUNS_DIR or --root). ``--crash-after`` kills the
worker abruptly right after that journal event, so ``resume`` can be shown picking up where it died.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from typing import Any

from incident_agent.durable.engine import Engine, LockError, RunState, WorkflowMismatch, crash_after
from incident_agent.durable.workflows import Deps, default_root, incident_workflow
from incident_agent.sandbox import select_backend


def _engine(args: argparse.Namespace, *, hook: Any = None) -> Engine:
    backend = args.backend if hasattr(args, "backend") else None
    deps = Deps(backend_factory=lambda: select_backend(backend))
    return Engine(incident_workflow(deps), args.root or default_root(), crash_hook=hook, poll_interval=0.2)


def describe(state: RunState, events: list[dict[str, Any]] | None = None) -> str:
    lines = [f"run {state.run_id}: {state.status}"]
    if state.waiting_for:
        lines.append(f"  waiting for: {state.waiting_for} (signal it with: signal {state.run_id} approval --by NAME)")
    if state.error:
        lines.append(f"  error: {state.error}")
    for name, result in state.results.items():
        if name == "report":
            lines.append("  report:\n    " + str(result.get("summary", "")).replace("\n", "\n    "))
        else:
            lines.append(f"  {name}: done")
    if state.skipped:
        lines.append(f"  skipped: {', '.join(state.skipped)}")
    if events is not None:
        started = [e for e in events if e["kind"] == "step_started"]
        counts: dict[str, int] = {}
        for e in started:
            counts[e["step"]] = counts.get(e["step"], 0) + 1
        lines.append(f"  step executions: {counts}")
    return "\n".join(lines)


def _show(engine: Engine, run_id: str) -> RunState:
    state = engine.state(run_id)
    print(describe(state, engine._journal(run_id).events()))  # noqa: SLF001
    return state


def _exit_code(state: RunState) -> int:
    return 1 if state.status in ("failed", "cancelled") else 0


def cmd_start(args: argparse.Namespace) -> int:
    hook = crash_after(args.crash_after) if args.crash_after else None
    engine = _engine(args, hook=hook)
    run_id = args.run_id or f"{args.incident.lower()}-{uuid.uuid4().hex[:6]}"
    engine.start(run_id, {"incident_id": args.incident})
    print(f"started {run_id}")
    state = engine.run(run_id, block=not args.no_wait)
    _show(engine, run_id)
    return _exit_code(state)


def cmd_resume(args: argparse.Namespace) -> int:
    engine = _engine(args)
    state = engine.run(args.run_id, block=not args.no_wait, retry_failed=args.retry_failed)
    _show(engine, args.run_id)
    return _exit_code(state)


def cmd_signal(args: argparse.Namespace) -> int:
    if args.name == "approval" and not args.by:
        print("an approval needs --by NAME (who approves)", file=sys.stderr)
        return 2
    payload = {"approved": not args.reject, "by": args.by} if args.name == "approval" else {}
    _engine(args).signal(args.run_id, args.name, payload)
    print(f"signal {args.name!r} sent to {args.run_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    engine = _engine(args)
    state = _show(engine, args.run_id)
    return _exit_code(state)


def cmd_cancel(args: argparse.Namespace) -> int:
    _engine(args).cancel(args.run_id)
    print(f"cancel requested for {args.run_id}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    engine = _engine(args)
    runs = engine.list_runs()
    for run_id in runs:
        print(f"{run_id}  {engine.state(run_id).status}")
    if not runs:
        print("no runs")
    return 0


def _child(args: Sequence[str], root: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "incident_agent.durable", "--root", root, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603


def cmd_demo(args: argparse.Namespace) -> int:
    """Kill a worker with SIGKILL mid-run, resume it, approve, and show that no finished step ran twice."""
    root = args.root or tempfile.mkdtemp(prefix="durable-demo-")
    run_id = "demo-1"
    print(f"runs directory: {root}\n1) start INC-002 and kill the worker right after 'diagnose' finished")
    first = _child(
        ["start", "INC-002", "--run-id", run_id, "--no-wait", "--crash-after", "step_completed:diagnose"], root
    )
    print(f"   worker exit code: {first.returncode} (negative on POSIX; nonzero indicates termination)")
    engine = Engine(incident_workflow(), root)
    before = engine.state(run_id)
    print(f"2) journal after the crash: status={before.status}, finished steps={list(before.results)}")
    print("3) resume in a fresh process: it stops at the human approval")
    paused = _child(["resume", run_id, "--no-wait"], root)
    print(paused.stdout.rstrip())
    print("4) a human approves, the run is resumed again")
    _child(["signal", run_id, "approval", "--by", "demo-operator"], root)
    done = _child(["resume", run_id, "--no-wait"], root)
    print(done.stdout.rstrip())
    counts: dict[str, int] = {}
    for e in engine._journal(run_id).events():  # noqa: SLF001
        if e["kind"] == "step_started":
            counts[e["step"]] = counts.get(e["step"], 0) + 1
    final = engine.state(run_id)
    ok = (
        first.returncode != 0
        and final.status == "completed"
        and set(before.results) <= set(final.results)
        and all(counts.get(name) == 1 for name in final.results if name != "approval")  # a wait is not a step run
    )
    print("RESULT:", "resumed without repeating any finished step" if ok else "FAILED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m incident_agent.durable", description=__doc__.split("\n")[0])
    p.add_argument("--root", default=os.environ.get("INCIDENT_RUNS_DIR"), help="directory for run journals")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("incident")
    s.add_argument("--run-id")
    s.add_argument("--no-wait", action="store_true", help="stop at the approval wait instead of polling")
    s.add_argument("--crash-after", metavar="EVENT:STEP")
    s.add_argument("--backend")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("resume")
    s.add_argument("run_id")
    s.add_argument("--no-wait", action="store_true")
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--backend")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("signal")
    s.add_argument("run_id")
    s.add_argument("name")
    s.add_argument("--by")
    s.add_argument("--reject", action="store_true")
    s.set_defaults(fn=cmd_signal)
    for name, fn in (("status", cmd_status), ("cancel", cmd_cancel)):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.set_defaults(fn=fn)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (KeyError, ValueError, LockError, WorkflowMismatch, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
