"""Temporal mapping of the incident workflow (Stage 17, optional: ``pip install "temporalio>=1.7"``).

The workflow only *orders* the steps and owns the waiting; one activity, ``incident_step``, runs the very same step
functions the dependency-free engine runs (``workflows.py``). Temporal then provides what the engine provides itself:
event history, deterministic replay, retries, durable timers and signals. ``execute`` is a single-attempt activity.

    temporal server start-dev                                  # a local dev server
    python -m incident_agent.durable.temporal worker
    python -m incident_agent.durable.temporal start INC-002
    python -m incident_agent.durable.temporal signal <workflow-id> --by alice
    python -m incident_agent.durable.temporal status <workflow-id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: F401  (kept for users who need it)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

with workflow.unsafe.imports_passed_through():
    from incident_agent.durable.engine import StepContext
    from incident_agent.durable.workflows import (
        APPROVAL_TTL,
        STEP_ORDER,
        Deps,
        default_root,
        make_diagnose,
        make_execute,
        make_triage,
        needs_approval,
        plan_action,
        report,
    )

TASK_QUEUE = "incident-response"
EXECUTE_ATTEMPTS, DEFAULT_ATTEMPTS = 1, 3


def attempts_for(step: str) -> int:
    """The mutating step is never retried by the platform (at most once); everything else is."""
    return EXECUTE_ATTEMPTS if step == "execute" else DEFAULT_ATTEMPTS


WORKFLOW_ID_PREFIX = "incident-"


def step_table(deps: Deps | None = None) -> dict[str, Callable[[StepContext], dict[str, Any]]]:
    d = deps or Deps()
    return {
        "triage": make_triage(d),
        "diagnose": make_diagnose(d),
        "plan_action": plan_action,
        "execute": make_execute(d),
        "report": report,
    }


def make_activity(deps: Deps | None = None) -> Callable[..., Any]:
    table = step_table(deps)

    @activity.defn(name="incident_step")
    async def incident_step(request: dict[str, Any]) -> dict[str, Any]:
        name = request["step"]
        if name not in table:
            raise ValueError(f"unknown step {name!r}")
        run_id = str(request["run_id"])
        run_dir = default_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        ctx = StepContext(run_id, name, activity.info().attempt, request["input"], request["results"], run_dir)
        return await asyncio.to_thread(table[name], ctx)

    return incident_step


@workflow.defn(name="IncidentWorkflow")
class IncidentWorkflow:
    def __init__(self) -> None:
        self._approval: dict[str, Any] | None = None
        self._cancelled = False

    @workflow.signal
    def approve(self, payload: dict[str, Any]) -> None:
        if self._approval is None:
            self._approval = payload

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True

    @workflow.query
    def progress(self) -> dict[str, Any]:
        return {"approval": self._approval, "cancelled": self._cancelled}

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        run_id = workflow.info().workflow_id
        results: dict[str, Any] = {}
        for name in STEP_ORDER:
            if self._cancelled:
                return {"status": "cancelled", "results": results}
            if name == "approval":
                if not needs_approval(results):
                    continue
                try:
                    await workflow.wait_condition(
                        lambda: self._approval is not None or self._cancelled,
                        timeout=timedelta(seconds=APPROVAL_TTL),
                    )
                    results[name] = {"timed_out": False, "payload": self._approval or {}}
                except TimeoutError:
                    results[name] = {"timed_out": True, "payload": {}}
                continue
            results[name] = await workflow.execute_activity(
                "incident_step",
                {"step": name, "run_id": run_id, "input": input, "results": results},
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=attempts_for(name), initial_interval=timedelta(seconds=1)),
            )
        return {"status": "completed", "results": results}


def sandbox_runner() -> SandboxedWorkflowRunner:
    """The workflow sandbox re-imports modules; the project package is passed through instead."""
    return SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules("incident_agent"))


def build_worker(client: Client, deps: Deps | None = None) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentWorkflow],
        activities=[make_activity(deps)],
        workflow_runner=sandbox_runner(),
    )


async def start_incident(client: Client, incident_id: str, workflow_id: str | None = None) -> str:
    wid = workflow_id or f"{WORKFLOW_ID_PREFIX}{incident_id.lower()}-{uuid.uuid4().hex[:6]}"
    await client.start_workflow(IncidentWorkflow.run, {"incident_id": incident_id}, id=wid, task_queue=TASK_QUEUE)
    return wid


async def _main(args: argparse.Namespace) -> int:
    client = await Client.connect(args.address)
    if args.cmd == "worker":
        print(f"worker on task queue {TASK_QUEUE!r} at {args.address} (Ctrl+C stops it; workflows resume on restart)")
        await build_worker(client).run()
    elif args.cmd == "start":
        print(await start_incident(client, args.incident, args.workflow_id))
    elif args.cmd == "signal":
        handle = client.get_workflow_handle(args.workflow_id)
        await handle.signal(IncidentWorkflow.approve, {"approved": not args.reject, "by": args.by})
        print("approval sent")
    elif args.cmd == "status":
        handle = client.get_workflow_handle(args.workflow_id)
        desc = await handle.describe()
        print(json.dumps({"status": desc.status.name if desc.status else None}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m incident_agent.durable.temporal")
    p.add_argument("--address", default=os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker")
    s = sub.add_parser("start")
    s.add_argument("incident")
    s.add_argument("--workflow-id")
    s = sub.add_parser("signal")
    s.add_argument("workflow_id")
    s.add_argument("--by", required=True)
    s.add_argument("--reject", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("workflow_id")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
