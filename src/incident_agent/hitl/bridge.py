"""Connecting the gate to durable runs (Stage 17 <-> Stage 18).

The incident workflow already pauses at a durable ``approval`` wait and treats anything but a clear, named approval as
a dry run. The controller turns that wait into a real human process without changing the workflow:

    run waits for 'approval'  ->  open a request (deadline = the run's own wait deadline)
    gate resolves the request ->  send the run an 'approval' signal (approved only when the humans approved)
    run stops waiting         ->  cancel a still-pending request

It is safe to run in several processes and to restart at any time: requests are event-sourced, opening is idempotent,
and delivering a decision twice is harmless (the wait consumes one signal, and both carry the same answer).
"""

from __future__ import annotations

from typing import Any

from incident_agent.durable.engine import Engine
from incident_agent.durable.journal import JournalError, LockError
from incident_agent.hitl.gate import ApprovalGate
from incident_agent.hitl.store import RequestState

WAIT_STEP = "approval"


def request_id_for(run_id: str) -> str:
    return f"{run_id}.approval"


def approval_payload(state: RequestState) -> dict[str, Any]:
    """What the run is told. Only an approved request says ``approved: True``."""
    if state.outcome == "approved":
        return {"approved": True, "by": "+".join(state.decided_by), "request": state.id}
    return {"approved": False, "by": "hitl-gate", "reason": str(state.outcome), "request": state.id}


class Controller:
    def __init__(self, gate: ApprovalGate, engine: Engine, *, drive: bool = True, signer: Any = None) -> None:
        self.gate, self.engine, self.drive, self.signer = gate, engine, drive, signer

    def _wait_deadline(self, run_id: str) -> float | None:
        from incident_agent.durable.journal import Journal

        waits = [e for e in Journal(self.engine.run_dir(run_id)).events() if e["kind"] == "wait_started"]
        mine = [e for e in waits if e.get("step") == WAIT_STEP]
        return float(mine[-1]["deadline"]) if mine else None

    def sync_runs(self, report: dict[str, list[Any]]) -> None:
        for run_id in self.engine.list_runs():
            rid = request_id_for(run_id)
            try:
                state = self.engine.state(run_id)
                if state.status == "waiting" and state.waiting_for == WAIT_STEP:
                    if not self.gate.store.exists(rid):
                        self._open(run_id, rid, state, report)
                elif self.gate.store.exists(rid) and self.gate.store.state(rid).pending:
                    self.gate.cancel(rid, f"the run is {state.status}, not waiting for approval")
                    report["cancelled"].append(rid)
            except (KeyError, ValueError, JournalError, LockError) as exc:
                report["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")

    def _open(self, run_id: str, rid: str, state: Any, report: dict[str, list[Any]]) -> None:
        plan = state.results.get("plan_action") or {}
        deadline = self._wait_deadline(run_id)
        requester = state.input.get("requested_by")
        self.gate.open(
            rid,
            run_id=run_id,
            incident_id=str(state.input.get("incident_id", "")),
            command_line=str(plan.get("command_line", "")),
            fingerprint=str(plan.get("fingerprint", "")),
            tier=str(plan.get("tier") or "high"),
            reason=str(state.results.get("diagnose", {}).get("evidence") or plan.get("reason") or "mutating action"),
            requested_by=requester if isinstance(requester, str) else None,
            hard_deadline=deadline,
        )
        report["opened"].append(rid)

    def deliver(self, report: dict[str, list[Any]]) -> None:
        for rid in self.gate.store.ids():
            st = self.gate.store.state(rid)
            if st.pending or st.delivered:
                continue
            try:
                run = self.engine.state(st.run_id)
                if run.status == "waiting" and run.waiting_for == WAIT_STEP:
                    payload = approval_payload(st)
                    if self.signer is not None:  # only the gate can seal a decision (Stage 20)
                        payload = self.signer.seal(st.run_id, st.fingerprint, payload)
                    self.engine.signal(st.run_id, WAIT_STEP, payload)
                self.gate.mark_delivered(rid)
                report["delivered"].append(rid)
                if self.drive:
                    self.engine.run(st.run_id, block=False)
            except LockError:
                continue  # another worker is running it right now; the decision is stored and tried again
            except (KeyError, ValueError, JournalError) as exc:
                report["errors"].append(f"{rid}: {type(exc).__name__}: {exc}")

    def step(self) -> dict[str, list[Any]]:
        report: dict[str, list[Any]] = {"opened": [], "cancelled": [], "ticked": [], "delivered": [], "errors": []}
        self.sync_runs(report)
        report["ticked"] = self.gate.tick()
        self.deliver(report)
        return report
