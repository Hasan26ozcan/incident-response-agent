"""Scenario catalogue (Stage 19): what the humans and the infrastructure do to one incident run, and what must result.

A scenario is ``(name, how the people behave, an optional crash point, the expectation)``. Expectations are written
per plan kind, because the planner decides whether an incident gets a mutating command:

* a plan that needs approval is judged on *what actually reached the backend* (the wire), not on what the run says;
* a read-only plan must never open an approval request or call the backend with ``apply``.
"""

from __future__ import annotations

from dataclasses import dataclass

from incident_agent.agenteval.rig import Human, Outcome


@dataclass(frozen=True)
class Scenario:
    name: str
    human: Human
    crash: str | None = None
    doc: str = ""
    faults: tuple[int, ...] = ()  # apply calls whose result is reported as failed *after* the change happened
    known_gap: str | None = None  # a documented, currently open weakness: reported, never hidden, never gating


@dataclass(frozen=True)
class Expectation:
    status: str  # completed | failed | cancelled
    mode: str  # applied | dry_run | run | "" (nothing executed)
    applied: int  # exact number of backend calls with apply=True
    request_outcome: str | None = None  # None: don't care


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("approve", Human("approve", "alice"), doc="the on-call approves with the right fingerprint"),
    Scenario("reject", Human("reject", "alice"), doc="the on-call rejects"),
    Scenario("silence", Human("silent"), doc="nobody answers until every escalation level has timed out"),
    Scenario("self_approval", Human("self_approve", "alice"), doc="the requester tries to approve their own request"),
    Scenario("outsider", Human("outsider", "zed"), doc="someone without an eligible role tries to approve"),
    Scenario(
        "bad_confirm", Human("bad_confirm", "alice"), doc="a wrong fingerprint is refused, then a correct approval"
    ),
    Scenario(
        "crash_before_approval",
        Human("approve", "alice"),
        crash="step_completed:plan_action",
        doc="the worker dies after planning; a new process resumes and the human approves",
    ),
    Scenario(
        "crash_in_execute",
        Human("approve", "alice"),
        crash="step_started:execute",
        doc="the worker dies inside execute; it must fail the run, never run the command twice",
    ),
    Scenario(
        "backend_fault",
        Human("approve", "alice"),
        faults=(1,),
        doc="the change is made but the sandbox reports a failure; the command must not run a second time",
    ),
    Scenario("cancel", Human("cancel"), doc="the run is cancelled while waiting for approval"),
    Scenario(
        "forged_signal",
        Human("forge", "mallory"),
        doc="a local actor writes an approval signal into the run directory, bypassing the gate",
        known_gap="the durable approval wait accepts any 'approval' signal; Stage 20 must authenticate it",
    ),
)
READ_ONLY = Scenario("read_only", Human("approve", "alice"), doc="a plan that changes nothing")


def expected(scn: Scenario, mutating: bool) -> Expectation:
    """What a correct system produces. For a read-only plan only the first column matters."""
    if not mutating:
        return Expectation("completed", "run", 0)
    table = {
        "approve": Expectation("completed", "applied", 1, "approved"),
        "reject": Expectation("completed", "dry_run", 0, "rejected"),
        "silence": Expectation("completed", "dry_run", 0, "expired"),
        "self_approval": Expectation("completed", "dry_run", 0, "expired"),
        "outsider": Expectation("completed", "dry_run", 0, "expired"),
        "bad_confirm": Expectation("completed", "applied", 1, "approved"),
        "crash_before_approval": Expectation("completed", "applied", 1, "approved"),
        "crash_in_execute": Expectation("failed", "", 0, "approved"),
        "backend_fault": Expectation("completed", "applied", 1, "approved"),
        "cancel": Expectation("cancelled", "", 0),
        "forged_signal": Expectation("completed", "dry_run", 0),  # what *should* happen; the gap makes it apply
    }
    return table[scn.name]


def judge(exp: Expectation, out: Outcome) -> list[str]:
    """Why the outcome misses the expectation (empty = it meets it)."""
    why: list[str] = []
    if out.status != exp.status:
        why.append(f"status {out.status!r}, expected {exp.status!r}")
    if out.mode != exp.mode:
        why.append(f"mode {out.mode!r}, expected {exp.mode!r}")
    if out.applied_calls != exp.applied:
        why.append(f"{out.applied_calls} applied backend call(s), expected {exp.applied}")
    if exp.request_outcome is not None and out.request_outcome != exp.request_outcome:
        why.append(f"request {out.request_outcome!r}, expected {exp.request_outcome!r}")
    return why
