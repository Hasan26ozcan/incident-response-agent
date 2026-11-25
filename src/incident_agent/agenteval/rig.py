"""Test rig for agentic evaluation (Stage 19): the real durable engine + approval gate, a simulated clock and humans.

Nothing here is a mock of the logic under test. The workflow, the sandbox runner, the gate, the roster and the
controller are the production classes; only three things are simulated:

* time (``SimClock``: sleeping advances a counter, so a 20 minute escalation takes microseconds),
* the isolation backend (``RecordingBackend`` runs the sandbox child program in a plain subprocess and records every
  call from the *outside*, so the safety metric does not trust the code it judges),
* the humans (``Human`` scripts what an on-call engineer, a bad actor or nobody does).

The backend declares full isolation but provides none; real isolation is Stage 16's selftest, not this stage's claim.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incident_agent.durable import workflows as wf
from incident_agent.durable.engine import Clock, Engine, RetryPolicy, RunState, Workflow
from incident_agent.hitl import (
    DEFAULT_POLICY,
    ApprovalGate,
    CallbackNotifier,
    Controller,
    GateError,
    Notification,
    RequestStore,
    Roster,
    request_id_for,
)
from incident_agent.sandbox.backends import (
    CAPS,
    ENV,
    FILESYSTEM,
    NETWORK,
    OUTPUT,
    PROCESSES,
    TIME,
    ExecResult,
    ExecSpec,
)
from incident_agent.security.killswitch import GuardedBackend, KillSwitch, guard_workflow
from incident_agent.security.signing import ApprovalSigner

START = 1_000_000.0
TERMINAL = ("completed", "failed", "cancelled")
ALL_GUARANTEES = frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS})
APPROVERS = {"alice": "oncall", "bob": "lead", "carol": "manager", "zed": "intern"}


class SimulatedCrash(BaseException):
    """A process death: not caught by ``except Exception`` anywhere in the engine."""


class SimClock(Clock):
    def __init__(self) -> None:
        self.t = START

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def __call__(self) -> float:  # the gate and store want a plain callable
        return self.t


class RecordingBackend:
    """Runs the sandbox child in a subprocess and records what the *wire* says, independent of any audit log."""

    name = "recording"

    def __init__(self, fail_apply_calls: tuple[int, ...] = ()) -> None:
        self.guarantees = ALL_GUARANTEES
        self.calls: list[dict[str, Any]] = []
        self._fail = set(fail_apply_calls)
        self._applies = 0

    def available(self) -> tuple[bool, str]:
        return True, "recording"

    @property
    def applied(self) -> int:
        return sum(1 for c in self.calls if c["apply"])

    def run(self, spec: ExecSpec) -> ExecResult:
        body = json.loads(spec.stdin)
        apply = body.get("apply") is True
        self.calls.append({"command": body.get("command"), "args": body.get("args"), "apply": apply})
        proc = subprocess.run(  # noqa: S603 - fixed argv, test rig only
            [sys.executable, "-I", "-S", "-B", "-c", spec.code], input=spec.stdin, capture_output=True, check=False
        )
        if apply:
            self._applies += 1
            if self._applies in self._fail:  # the change happened, but the caller is told it failed
                return ExecResult(1, proc.stdout, b"injected fault after the change", backend=self.name)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr, backend=self.name)


@dataclass
class Human:
    """What the people do. ``kind`` picks the script; ``who`` is the actor."""

    kind: str = "approve"  # approve | reject | silent | self_approve | outsider | bad_confirm | forge | cancel
    who: str = "alice"


@dataclass
class Outcome:
    scenario: str
    incident_id: str
    status: str = ""
    mode: str = ""  # applied | dry_run | run | "" (no execute result)
    applied_calls: int = 0
    backend_calls: int = 0
    approved_by: str | None = None
    request_outcome: str | None = None
    refusals: list[str] = field(default_factory=list)
    events: int = 0
    sim_seconds: float = 0.0
    notifications: int = 0
    crashed: int = 0
    gate_approved: bool = False
    error: str | None = None


class Rig:
    def __init__(
        self,
        root: Path,
        *,
        workflow_factory: Callable[[wf.Deps], Workflow] = wf.incident_workflow,
        backend: RecordingBackend | None = None,
        data_loader: Callable[[str], dict[str, Any]] | None = None,
        retry: RetryPolicy | None = None,
        policy: Any = DEFAULT_POLICY,
        killswitch: KillSwitch | None = None,
    ) -> None:
        self.root, self.clock = Path(root), SimClock()
        self.signer = ApprovalSigner(secrets.token_bytes(32))  # the gate's key; the worker verifies with it (Stage 20)
        self.backend = backend or RecordingBackend()
        self.policy = policy
        self.killswitch = killswitch
        self.deps = wf.Deps(
            backend_factory=lambda: GuardedBackend(self.backend, killswitch) if killswitch else self.backend,
            data_loader=data_loader,
            retry=retry or RetryPolicy(max_attempts=3, initial_delay=0.2, max_delay=2.0),
            approval_timeout=policy.total_timeout,
            signer=self.signer,
        )
        self.workflow_factory = workflow_factory
        self.roster = Roster(self.root / "roster.json")
        self.tokens = {n: self.roster.add(n, [r]) for n, r in APPROVERS.items()}
        self.notes: list[Notification] = []
        self.gate = ApprovalGate(
            RequestStore(self.root / "requests", self.clock, seal_key=secrets.token_bytes(32)),
            self.roster,
            policy,
            CallbackNotifier(lambda n: self.notes.append(n)),
            self.clock,
        )
        self.engine = self._engine()
        self.ctl = Controller(self.gate, self.engine, signer=self.signer)

    def _engine(self, crash_hook: Callable[[dict[str, Any]], None] | None = None) -> Engine:
        flow = self.workflow_factory(self.deps)
        if self.killswitch is not None:
            flow = guard_workflow(flow, self.killswitch)
        return Engine(
            flow,
            self.root / "runs",
            clock=self.clock,
            crash_hook=crash_hook,
            poll_interval=0.5,
        )

    # ------------------------------------------------------------------ crash and restart

    def arm_crash(self, spec: str) -> None:
        kind, _, step = spec.partition(":")

        def hook(event: dict[str, Any]) -> None:
            if event["kind"] == kind and (not step or event.get("step") == step):
                raise SimulatedCrash(spec)

        self.engine = self._engine(hook)
        self.ctl = Controller(self.gate, self.engine, signer=self.signer)

    def restart(self) -> None:
        """A fresh process: new engine and controller over the same journals, same stores."""
        self.engine = self._engine()
        self.ctl = Controller(self.gate, self.engine, signer=self.signer)


def run_scenario(
    rig: Rig,
    scenario: str,
    incident_id: str,
    human: Human,
    *,
    crash: str | None = None,
    max_ticks: int = 80,
) -> Outcome:
    out = Outcome(scenario, incident_id)
    run_id = f"{scenario}-{incident_id}".lower()
    rid = request_id_for(run_id)
    requester = "alice" if human.kind == "self_approve" else None
    started = rig.clock.t
    rig.engine.start(run_id, {"incident_id": incident_id, **({"requested_by": requester} if requester else {})})
    if crash:
        rig.arm_crash(crash)

    def tick() -> None:
        # a crash can strike inside the worker loop or inside the controller driving a run; either way the next
        # "process" is a fresh engine over the same journals, which is exactly what the engine must survive
        try:
            rig.engine.run(run_id, block=False)
            rig.ctl.step()
        except SimulatedCrash:
            out.crashed += 1
            rig.restart()

    acted = False
    for _ in range(max_ticks):
        tick()
        st = rig.engine.state(run_id)
        if st.status in TERMINAL:
            break
        if st.status == "waiting" and not acted and rig.gate.store.exists(rid) and rig.gate.store.state(rid).pending:
            acted = _act(rig, human, run_id, rid, out)
            if acted:
                continue
        rig.clock.t += 30.0
    st = _finish(rig, run_id, rid, out)
    out.sim_seconds = rig.clock.t - started
    out.status = st.status
    return out


def _act(rig: Rig, human: Human, run_id: str, rid: str, out: Outcome) -> bool:
    state = rig.gate.store.state(rid)
    confirm = state.fingerprint[:8]
    token = rig.tokens.get(human.who, "")
    try:
        if human.kind == "approve":
            rig.gate.approve(rid, human.who, token, confirm=confirm)
        elif human.kind == "reject":
            rig.gate.reject(rid, human.who, token)
        elif human.kind == "silent":
            rig.clock.t += rig.policy.total_timeout + 1.0
        elif human.kind in ("self_approve", "outsider"):
            rig.gate.approve(rid, human.who, token, confirm=confirm)
        elif human.kind == "bad_confirm":
            rig.gate.approve(rid, human.who, token, confirm="deadbeef")
        elif human.kind == "forge":
            rig.engine.signal(run_id, "approval", {"approved": True, "by": human.who})
        elif human.kind == "cancel":
            rig.engine.cancel(run_id)
    except GateError as exc:
        out.refusals.append(type(exc).__name__)
        if human.kind == "bad_confirm":  # the honest second attempt, with the right fingerprint
            rig.gate.approve(rid, "alice", rig.tokens["alice"], confirm=confirm)
        elif human.kind in ("self_approve", "outsider"):
            rig.clock.t += rig.policy.total_timeout + 1.0  # nobody else answers
    return True


def _finish(rig: Rig, run_id: str, rid: str, out: Outcome) -> RunState:
    st = rig.engine.state(run_id)
    ex = st.results.get("execute") or {}
    out.mode = str(ex.get("mode", ""))
    out.approved_by = ex.get("approved_by")
    out.error = st.error
    out.applied_calls = rig.backend.applied
    out.backend_calls = len(rig.backend.calls)
    if rig.gate.store.exists(rid):
        rs = rig.gate.store.state(rid)
        out.request_outcome = str(rs.outcome)
        out.gate_approved = rs.outcome == "approved"
    out.events = st.events
    out.notifications = len(rig.notes)
    return st
