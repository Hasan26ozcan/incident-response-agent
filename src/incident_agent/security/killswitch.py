"""Kill-switch (Stage 20): one switch that stops the agent from changing anything, and that the agent cannot undo.

Design rules
* **Stopping is easy, resuming is hard.** Anyone (or any monitor) may ``engage``; ``release`` needs the operator token,
  whose SHA-256 digest is stored next to the switch. With no operator configured the switch cannot be released at all.
* **Fail closed.** An unreadable or corrupt state file counts as engaged. ``INCIDENT_KILL`` in the environment engages
  the switch independently of any file (an out-of-band stop that file access cannot clear).
* **Enforced at the bottom.** ``GuardedBackend`` refuses to run *anything* while engaged (reads included), and every
  workflow step is wrapped to fail the run (non-retryable) instead of starting. ``halt_runs`` cancels in-flight runs.
  The switch is checked on every call, never cached, so engaging takes effect on the very next step or command.
* **Everything is journalled** (``history.jsonl``): who engaged, who released, and every refused release.

Limit: a local actor who can write the state directory can still delete it; that is a file-permission problem the
deployment owns, and the environment variable exists for exactly that situation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from incident_agent.durable.engine import Engine, NonRetryable, Step, Workflow
from incident_agent.sandbox.backends import Backend, ExecResult, ExecSpec

ENV = "INCIDENT_KILL"
TERMINAL = ("completed", "failed", "cancelled")


class KillSwitchEngaged(NonRetryable):
    """Raised instead of doing work while the switch is engaged (non-retryable: the run fails at once)."""


class KillSwitchError(RuntimeError):
    pass


@dataclass(frozen=True)
class KillState:
    engaged: bool
    source: str  # none | file | env | corrupt
    reason: str = ""
    by: str = ""
    at: float = 0.0


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class KillSwitch:
    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.clock = clock
        self.state_file = self.dir / "killswitch.json"
        self.operator_file = self.dir / "operator.json"
        self.history = self.dir / "history.jsonl"

    # ------------------------------------------------------------------ reading (never cached, fail closed)

    def state(self) -> KillState:
        if os.environ.get(ENV, "").strip().lower() not in ("", "0", "false", "no"):
            return KillState(True, "env", f"{ENV} is set")
        try:
            raw = self.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return KillState(False, "none")
        except OSError:
            return KillState(True, "corrupt", "the switch state cannot be read")
        try:
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get("engaged"), bool):
                raise ValueError
            return KillState(
                row["engaged"], "file", str(row.get("reason", "")), str(row.get("by", "")), float(row.get("at", 0.0))
            )
        except (ValueError, TypeError):
            return KillState(True, "corrupt", "the switch state is corrupt")

    def engaged(self) -> bool:
        return self.state().engaged

    def check(self) -> None:
        st = self.state()
        if st.engaged:
            raise KillSwitchEngaged(f"kill switch engaged ({st.source}): {st.reason or 'no reason given'}")

    # ------------------------------------------------------------------ writing

    def _write_state(self, row: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".ks-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_file)  # atomic: a reader sees the old or the new state, never half of one
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _log(self, event: str, **fields: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": self.clock(), "event": event, **fields}, sort_keys=True) + "\n"
        fd = os.open(self.history, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def engage(self, reason: str, by: str) -> KillState:
        if not isinstance(by, str) or not by.strip():
            raise KillSwitchError("say who engages the switch")
        reason = (reason or "").strip()[:500]
        self._write_state({"engaged": True, "reason": reason, "by": by.strip()[:100], "at": self.clock()})
        self._log("engaged", by=by.strip()[:100], reason=reason)
        return self.state()

    def set_operator(self, token: str) -> None:
        """Choose who may release (first time only; changing the operator needs the current token)."""
        if not isinstance(token, str) or len(token) < 16:
            raise KillSwitchError("the operator token must be at least 16 characters")
        if self.operator_file.exists():
            raise KillSwitchError("an operator is already configured; delete operator.json deliberately to replace it")
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.operator_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"digest": _digest(token)}))
        self._log("operator_configured")

    def release(self, by: str, token: str) -> KillState:
        try:
            stored = json.loads(self.operator_file.read_text(encoding="utf-8")).get("digest")
        except (OSError, ValueError, AttributeError):
            stored = None
        if not stored:
            self._log("release_refused", by=str(by)[:100], why="no operator configured")
            raise KillSwitchError("no operator is configured, so the switch cannot be released")
        ok = hmac.compare_digest(_digest(token if isinstance(token, str) else ""), str(stored))
        if not ok or not isinstance(by, str) or not by.strip():
            self._log("release_refused", by=str(by)[:100], why="bad token or no name")
            raise KillSwitchError("release refused")
        if self.state().source == "env":
            raise KillSwitchError(f"{ENV} is set in the environment; unset it first")
        self._write_state({"engaged": False, "reason": "", "by": by.strip()[:100], "at": self.clock()})
        self._log("released", by=by.strip()[:100])
        return self.state()


class GuardedBackend:
    """Wraps a sandbox backend: while the switch is engaged nothing runs, not even a read."""

    def __init__(self, inner: Backend, switch: KillSwitch) -> None:
        self.inner, self.switch = inner, switch
        self.name = inner.name
        self.guarantees = inner.guarantees

    def available(self) -> tuple[bool, str]:
        return self.inner.available()

    def run(self, spec: ExecSpec) -> ExecResult:
        self.switch.check()
        return self.inner.run(spec)


def guard_workflow(flow: Workflow, switch: KillSwitch) -> Workflow:
    """Every step checks the switch first; waits are left alone (a woken wait leads into a guarded step)."""

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(ctx: Any) -> Any:
            switch.check()
            return fn(ctx)

        return run

    steps: list[Any] = [replace(s, fn=guarded(s.fn)) if isinstance(s, Step) else s for s in flow.steps]
    return Workflow(flow.name, flow.version, tuple(steps))


def halt_runs(engine: Engine, switch: KillSwitch, *, reason: str = "") -> list[str]:
    """Cancel every run that is still alive. Returns the ids signalled. Safe to call repeatedly."""
    halted = []
    for run_id in engine.list_runs():
        if engine.state(run_id).status not in TERMINAL:
            engine.cancel(run_id)
            halted.append(run_id)
    if halted:
        switch._log("runs_halted", runs=halted, reason=reason)  # noqa: SLF001
    return halted


def engage_and_halt(switch: KillSwitch, engine: Engine, reason: str, by: str) -> list[str]:
    """The emergency button: stop new work first, then cancel what is running."""
    switch.engage(reason, by)
    return halt_runs(engine, switch, reason=reason)
