"""A small durable-execution engine (Stage 17): a workflow is an ordered list of steps whose results are journalled.

If the process dies at any point, ``Engine.run`` on the same run id replays the journal, never re-runs a step that
already completed, and continues from the first unfinished one. This is the same idea Temporal implements (event
history, deterministic replay, activities, signals, timers, retries); ``temporal.py`` maps the incident workflow onto
real Temporal, this module is the dependency-free engine that is tested end to end, including a real SIGKILL.

Guarantees and limits (read before relying on it):

* a completed step is never executed again; a step that was *started* but not completed when the process died is
  executed again, so steps are **at-least-once** and must be idempotent (``StepContext.idempotency_key`` is stable);
* attempts are counted across crashes, so a step that keeps killing its worker still ends in ``failed``;
* the step list must match the one the run started with (name and version): resuming against a changed workflow is
  refused instead of guessed (Temporal calls this a non-determinism error);
* a step timeout abandons a thread, it cannot kill it; use process isolation (Stage 16) for hard limits.
"""

from __future__ import annotations

import json
import os
import signal as _signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incident_agent.durable.journal import (
    Journal,
    JournalError,
    LockError,
    RunLock,
    check_run_id,
    read_signals,
    write_signal,
)
from incident_agent.sandbox.audit import one_line

MAX_RESULT_BYTES = 256_000
TERMINAL = frozenset({"completed", "failed", "cancelled"})


class WorkflowMismatch(RuntimeError):
    """The journal was written by a different workflow definition than the one asked to resume it."""


class StepTimeout(Exception):
    pass


class NonRetryable(Exception):
    """Raise from a step to fail the run at once, without further attempts."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 0.5
    backoff: float = 2.0
    max_delay: float = 10.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.initial_delay < 0 or self.backoff < 1 or self.max_delay < 0:
            raise ValueError("invalid retry policy")

    def delay(self, attempt: int) -> float:
        return float(min(self.max_delay, self.initial_delay * self.backoff ** max(attempt - 1, 0)))


@dataclass(frozen=True)
class StepContext:
    run_id: str
    step: str
    attempt: int
    input: dict[str, Any]
    results: dict[str, Any]
    run_dir: Path

    @property
    def idempotency_key(self) -> str:
        return f"{self.run_id}:{self.step}"


@dataclass(frozen=True)
class Step:
    name: str
    fn: Callable[[StepContext], Any]
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: float | None = None
    when: Callable[[dict[str, Any]], bool] | None = None  # receives prior results; False records the step as skipped


@dataclass(frozen=True)
class Wait:
    """Pause until a signal arrives (or the deadline passes). The deadline is journalled, so it survives a restart."""

    name: str
    signal: str
    timeout: float
    when: Callable[[dict[str, Any]], bool] | None = None


@dataclass(frozen=True)
class Workflow:
    name: str
    version: str
    steps: tuple[Step | Wait, ...]

    def __post_init__(self) -> None:
        names = [s.name for s in self.steps]
        if not names or len(set(names)) != len(names):
            raise ValueError("a workflow needs at least one step and unique step names")


@dataclass(frozen=True)
class RunState:
    run_id: str
    status: str  # running | waiting | completed | failed | cancelled
    results: dict[str, Any]
    skipped: tuple[str, ...]
    error: str | None
    waiting_for: str | None
    events: int
    input: dict[str, Any]


class Clock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def kill_self(event: dict[str, Any]) -> None:  # pragma: no cover - the process dies here, by design
    """Crash hook for demos and tests: terminate abruptly without cleanup or atexit handlers."""
    if os.name == "nt":
        os._exit(137)
    os.kill(os.getpid(), _signal.SIGKILL)  # type: ignore[attr-defined]


def crash_after(spec: str) -> Callable[[dict[str, Any]], None]:
    """``"step_completed:diagnose"`` -> a hook that kills the process right after that journal event."""
    kind, _, step = spec.partition(":")

    def hook(event: dict[str, Any]) -> None:
        if event["kind"] == kind and (not step or event.get("step") == step):
            kill_self(event)

    return hook


class Engine:
    def __init__(
        self,
        workflow: Workflow,
        root: str | Path,
        *,
        clock: Clock | None = None,
        crash_hook: Callable[[dict[str, Any]], None] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.workflow = workflow
        self.root = Path(root)
        self.clock = clock or Clock()
        self._crash_hook = crash_hook
        self._poll = poll_interval

    # -- paths ----------------------------------------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.root / check_run_id(run_id)

    def _journal(self, run_id: str) -> Journal:
        return Journal(self.run_dir(run_id), self.clock.now)

    def _append(self, journal: Journal, kind: str, **fields: Any) -> dict[str, Any]:
        event = journal.append(kind, **fields)
        if self._crash_hook:
            self._crash_hook(event)
        return event

    # -- public API -------------------------------------------------------------------------------------------------

    def start(self, run_id: str, input: dict[str, Any]) -> str:
        """Create a run. Calling it again with the same input is a no-op (safe to retry after a crash)."""
        journal = self._journal(run_id)
        if journal.exists():
            first = journal.events()[0]
            if first.get("kind") != "run_started" or first.get("input") != json.loads(json.dumps(input)):
                raise ValueError(f"run {run_id!r} already exists with a different input")
            return run_id
        self._append(
            journal,
            "run_started",
            workflow=self.workflow.name,
            version=self.workflow.version,
            steps=[s.name for s in self.workflow.steps],
            input=json.loads(json.dumps(input)),
        )
        return run_id

    def state(self, run_id: str) -> RunState:
        events = self._journal(run_id).events()
        if not events:
            raise KeyError(f"unknown run {run_id!r}")
        return self._fold(run_id, events)[0]

    def signal(self, run_id: str, name: str, payload: dict[str, Any] | None = None) -> str:
        if not self._journal(run_id).exists():
            raise KeyError(f"unknown run {run_id!r}")
        return write_signal(self.run_dir(run_id), name, payload)

    def cancel(self, run_id: str) -> str:
        return self.signal(run_id, "cancel", {})

    def list_runs(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "journal.jsonl").exists())

    def run(self, run_id: str, *, block: bool = True, retry_failed: bool = False) -> RunState:
        """Run (or resume) until the workflow ends, or - with ``block=False`` - until it has to wait for a signal."""
        journal = self._journal(run_id)
        if not journal.exists():
            raise KeyError(f"unknown run {run_id!r}")
        lock = RunLock(self.run_dir(run_id))
        lock.acquire()
        try:
            return self._drive(run_id, journal, block, retry_failed)
        finally:
            lock.release()

    # -- replay -----------------------------------------------------------------------------------------------------

    def _fold(self, run_id: str, events: list[dict[str, Any]]) -> tuple[RunState, dict[str, Any]]:
        first = events[0]
        results: dict[str, Any] = {}
        skipped: list[str] = []
        status, error, waiting = "running", None, None
        deadlines: dict[str, float] = {}
        for e in events:
            kind, step = e["kind"], str(e.get("step"))
            if kind == "step_completed":
                results[step] = e.get("result")
            elif kind == "step_skipped":
                skipped.append(step)
            elif kind == "wait_started":
                deadlines[step] = e["deadline"]
                waiting = step
            elif kind == "run_resumed":
                status, error = "running", None
            elif kind == "run_completed":
                status = "completed"
            elif kind == "run_failed":
                status, error = "failed", e.get("error")
            elif kind == "run_cancelled":
                status = "cancelled"
            if kind in ("step_completed", "step_skipped") and waiting == step:
                waiting = None
        info = {
            "deadlines": deadlines,
        }
        state = RunState(
            run_id=run_id,
            status="waiting" if status == "running" and waiting else status,
            results=results,
            skipped=tuple(skipped),
            error=error,
            waiting_for=waiting if status == "running" else None,
            events=len(events),
            input=first.get("input", {}),
        )
        return state, info

    def _check_definition(self, events: list[dict[str, Any]]) -> None:
        first = events[0]
        names = [s.name for s in self.workflow.steps]
        if (first.get("workflow"), first.get("version"), first.get("steps")) != (
            self.workflow.name,
            self.workflow.version,
            names,
        ):
            raise WorkflowMismatch(
                f"run was started by {first.get('workflow')!r} v{first.get('version')} with steps "
                f"{first.get('steps')}; this is {self.workflow.name!r} v{self.workflow.version} with {names}"
            )

    # -- execution --------------------------------------------------------------------------------------------------

    def _drive(self, run_id: str, journal: Journal, block: bool, retry_failed: bool) -> RunState:
        events = journal.events()
        self._check_definition(events)
        state, info = self._fold(run_id, events)
        if state.status == "failed" and retry_failed:
            self._append(journal, "run_resumed", reason="retry_failed")
            state, info = self._fold(run_id, journal.events())
        if state.status in TERMINAL:
            return state
        for step in self.workflow.steps:
            state, info = self._fold(run_id, journal.events())
            if step.name in state.results or step.name in state.skipped:
                continue
            self._ingest_signals(run_id, journal)
            if self._cancel_requested(journal):
                self._append(journal, "run_cancelled")
                return self.state(run_id)
            if step.when is not None and not step.when(dict(state.results)):
                self._append(journal, "step_skipped", step=step.name)
                continue
            ok = (
                self._wait(run_id, journal, step, block)
                if isinstance(step, Wait)
                else self._step(run_id, journal, step)
            )
            if not ok:
                return self.state(run_id)
        if self.state(run_id).status == "running":
            final = self.state(run_id)
            self._append(journal, "run_completed", result=final.results.get(self.workflow.steps[-1].name))
        return self.state(run_id)

    def _ingest_signals(self, run_id: str, journal: Journal) -> None:
        known = {e["id"] for e in journal.events() if e["kind"] == "signal_received"}
        for sig in read_signals(self.run_dir(run_id)):
            if sig["id"] not in known:
                self._append(journal, "signal_received", id=sig["id"], name=sig["name"], payload=sig["payload"])
                known.add(sig["id"])

    def _cancel_requested(self, journal: Journal) -> bool:
        return any(e["kind"] == "signal_received" and e["name"] == "cancel" for e in journal.events())

    def _wait(self, run_id: str, journal: Journal, step: Wait, block: bool) -> bool:
        _, info = self._fold(run_id, journal.events())
        if step.name not in info["deadlines"]:
            self._append(
                journal, "wait_started", step=step.name, signal=step.signal, deadline=self.clock.now() + step.timeout
            )
            _, info = self._fold(run_id, journal.events())
        deadline = info["deadlines"][step.name]
        while True:
            self._ingest_signals(run_id, journal)
            events = journal.events()
            if self._cancel_requested(journal):
                self._append(journal, "run_cancelled")
                return False
            hit = next((e for e in events if e["kind"] == "signal_received" and e["name"] == step.signal), None)
            if hit is not None:
                self._append(
                    journal, "step_completed", step=step.name, result={"timed_out": False, "payload": hit["payload"]}
                )
                return True
            if self.clock.now() >= deadline:
                self._append(journal, "wait_timed_out", step=step.name)
                self._append(journal, "step_completed", step=step.name, result={"timed_out": True, "payload": {}})
                return True
            if not block:
                return False
            self.clock.sleep(self._poll)

    def _step(self, run_id: str, journal: Journal, step: Step) -> bool:
        while True:
            events = journal.events()
            resumed_at = max((e["seq"] for e in events if e["kind"] == "run_resumed"), default=0)
            mine = [e for e in events if e.get("step") == step.name and e["seq"] > resumed_at]
            attempt = sum(1 for e in mine if e["kind"] == "step_started")
            if attempt >= step.retry.max_attempts:
                msg = f"{step.name} used all {step.retry.max_attempts} attempts"
                self._append(journal, "run_failed", step=step.name, error=msg)
                return False
            interrupted = bool(mine) and mine[-1]["kind"] == "step_started"  # the worker died inside this step
            number = attempt + 1
            self._append(journal, "step_started", step=step.name, attempt=number, interrupted_before=interrupted)
            state, _ = self._fold(run_id, journal.events())
            ctx = StepContext(run_id, step.name, number, state.input, dict(state.results), self.run_dir(run_id))
            try:
                blob = json.dumps(self._call(step, ctx), sort_keys=True)
                if len(blob) > MAX_RESULT_BYTES:
                    raise NonRetryable(f"result of {step.name} is larger than {MAX_RESULT_BYTES} bytes")
            except NonRetryable as exc:
                return self._fail_for_good(journal, step, number, str(exc))
            except (TypeError, ValueError) as exc:
                if "JSON serializable" in str(exc) or "Circular" in str(exc):
                    return self._fail_for_good(journal, step, number, f"result of {step.name} is not JSON serialisable")
                if not self._failed(journal, step, number, exc):
                    return False
                continue
            except Exception as exc:  # noqa: BLE001 - a step may fail in any way; the journal records it
                if not self._failed(journal, step, number, exc):
                    return False
                continue
            self._append(journal, "step_completed", step=step.name, attempt=number, result=json.loads(blob))
            return True

    def _fail_for_good(self, journal: Journal, step: Step, attempt: int, error: str) -> bool:
        msg = one_line(error)
        self._append(journal, "step_failed", step=step.name, attempt=attempt, error=msg, retryable=False)
        self._append(journal, "run_failed", step=step.name, error=msg)
        return False

    def _failed(self, journal: Journal, step: Step, attempt: int, exc: BaseException) -> bool:
        """Record a failure; True when the step should be retried (after the backoff delay)."""
        message = f"{type(exc).__name__}: {one_line(str(exc))}"
        retry = attempt < step.retry.max_attempts
        self._append(journal, "step_failed", step=step.name, attempt=attempt, error=message, retryable=retry)
        if not retry:
            self._append(journal, "run_failed", step=step.name, error=message)
            return False
        self.clock.sleep(step.retry.delay(attempt))
        return True

    def _call(self, step: Step, ctx: StepContext) -> Any:
        if step.timeout is None:
            return step.fn(ctx)
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = step.fn(ctx)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(step.timeout)
        if thread.is_alive():
            raise StepTimeout(f"{step.name} did not finish within {step.timeout:g}s")
        if "error" in box:
            raise box["error"]
        return box["result"]


__all__ = [
    "Clock",
    "Engine",
    "JournalError",
    "LockError",
    "NonRetryable",
    "RetryPolicy",
    "RunState",
    "Step",
    "StepContext",
    "StepTimeout",
    "Wait",
    "Workflow",
    "WorkflowMismatch",
    "crash_after",
]
