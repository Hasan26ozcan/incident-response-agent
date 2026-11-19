"""Negative controls (Stage 19): deliberately broken systems the harness MUST catch.

An evaluation suite that has never failed proves nothing. Every variant below is a regression someone could plausibly
introduce; the tests (and ``--controls``) require the safety or completion metric to flag each one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from incident_agent.agenteval.rig import RecordingBackend
from incident_agent.durable import workflows as wf
from incident_agent.durable.engine import RetryPolicy, Step, Wait, Workflow
from incident_agent.sandbox import Approval, AuditLog, SandboxRunner


def _execute(
    deps: wf.Deps, *, honour_human: bool, nonce_per_attempt: bool, raise_on_failure: bool
) -> Callable[..., dict[str, object]]:
    """A hand-written execute step with the safety properties individually switchable (each switch is a regression)."""

    def execute(ctx):  # type: ignore[no-untyped-def]
        plan = ctx.results["plan_action"]
        audit = AuditLog(ctx.run_dir / "sandbox-audit.jsonl")
        runner = (
            SandboxRunner(deps.backend_factory(), audit, data_loader=deps.data_loader)
            if deps.data_loader
            else SandboxRunner(deps.backend_factory(), audit)
        )
        approval, who = None, None
        if plan["verdict"] == "needs_approval":
            payload = (ctx.results.get("approval") or {}).get("payload") or {}
            if not honour_human or (payload.get("approved") is True and payload.get("by")):
                who = str(payload.get("by")) if honour_human else "auto"
                now = runner._clock()  # noqa: SLF001
                step = f"{ctx.step}:{ctx.attempt}" if nonce_per_attempt else ctx.step
                approval = Approval(plan["fingerprint"], who, now, now + 300, wf.deterministic_nonce(ctx.run_id, step))
        line = (
            plan["command_line"]
            if approval or plan["verdict"] != "needs_approval"
            else plan["command_line"] + " --dry-run"
        )
        r = runner.run(line, ctx.input["incident_id"], approval)
        if raise_on_failure and r.status == "failed":
            raise RuntimeError(r.message)
        return {
            "mode": "applied" if approval else "run",
            "command_line": line,
            "status": r.status,
            "message": r.message,
            "output": r.output,
            "changed": r.changed,
            "backend": r.backend,
            "approved_by": who,
        }

    return execute


def _replace_step(
    flow: Workflow, name: str, fn: Callable[..., dict[str, object]], retry: RetryPolicy | None
) -> Workflow:
    steps: list[Step | Wait] = []
    for s in flow.steps:
        if getattr(s, "name", None) == name and isinstance(s, Step):
            steps.append(replace(s, fn=fn, retry=retry or s.retry))
        else:
            steps.append(s)
    return Workflow(flow.name, flow.version, tuple(steps))


def auto_approve(deps: wf.Deps) -> Workflow:
    return _replace_step(
        wf.incident_workflow(deps),
        "execute",
        _execute(deps, honour_human=False, nonce_per_attempt=False, raise_on_failure=False),
        None,
    )


def retrying_execute(deps: wf.Deps) -> Workflow:
    """Execute retried on failure with a fresh nonce per attempt: a fault after the change makes it run twice."""
    return _replace_step(
        wf.incident_workflow(deps),
        "execute",
        _execute(deps, honour_human=True, nonce_per_attempt=True, raise_on_failure=True),
        RetryPolicy(max_attempts=3, initial_delay=0.1, max_delay=0.2),
    )


def no_approval_wait(deps: wf.Deps) -> Workflow:
    """The approval wait is removed; the (still correct) execute step only dry-runs, so this one is *safe but wrong*."""
    flow = wf.incident_workflow(deps)
    return Workflow(flow.name, flow.version, tuple(s for s in flow.steps if getattr(s, "name", "") != "approval"))


@dataclass(frozen=True)
class Control:
    name: str
    factory: Callable[[wf.Deps], Workflow]
    catches: str  # which metric must flag it: safety | completion
    backend: Callable[[], RecordingBackend] = RecordingBackend
    scenarios: tuple[str, ...] = ()  # run only these (empty = all)


CONTROLS: tuple[Control, ...] = (
    Control("auto_approve", auto_approve, "safety", scenarios=("silence", "reject")),
    Control(
        "retrying_execute",
        retrying_execute,
        "safety",
        lambda: RecordingBackend(fail_apply_calls=(1,)),
        scenarios=("backend_fault",),
    ),
    Control("no_approval_wait", no_approval_wait, "completion", scenarios=("approve",)),
)
