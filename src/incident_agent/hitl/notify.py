"""Telling humans (Stage 18): the message for each moment of a request, and where it goes.

A notifier never decides anything. A failed delivery is recorded and retried on the next tick (bounded), and the
request's state shows it; it can neither approve nor lose a request. Messages are built from sanitised text only: the
command line, incident id and comments pass through the same redaction/escaping the Stage 15 reports use.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from incident_agent.hitl.store import RequestState
from incident_agent.reporting.sanitize import one_line, slack_text
from incident_agent.reporting.slack import Sender, validate_mention

CONFIRM_LEN = 8  # an approver types the first characters of the fingerprint: "I read what I am approving"


@dataclass(frozen=True)
class Notification:
    kind: str  # opened | escalated | resolved
    request_id: str
    level: int
    level_name: str
    roles: tuple[str, ...]
    text: str  # plain, single message, already sanitised
    mention: str | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    detail: str = ""


class Notifier(Protocol):
    name: str

    def send(self, note: Notification) -> NotifyResult: ...


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_notification(state: RequestState, kind: str) -> Notification:
    level = state.levels[state.level]
    roles = tuple(str(r) for r in level["roles"])
    cmd = one_line(state.command_line, 200)
    head = f"[{one_line(state.incident_id, 40)}] approval for high-risk action"
    fp = state.fingerprint[:CONFIRM_LEN]
    if kind == "resolved":
        who = ", ".join(state.decided_by) or "nobody"
        text = (
            f"{head}: {state.outcome} ({one_line(state.resolved_reason, 160)}). Action `{cmd}`. "
            f"By: {one_line(who, 120)}."
            + (" Nothing was executed on approval grounds." if state.outcome != "approved" else "")
        )
        return Notification("resolved", state.id, state.level, state.level_name, roles, text, None, state.outcome)
    lead = "ESCALATED to " if kind == "escalated" else "Needs a human: "
    text = (
        f"{head}. {lead}level '{level['name']}' (roles: {', '.join(roles)}). Action `{cmd}` on "
        f"{one_line(state.incident_id, 40)}. Why: {one_line(state.reason, 200)}. Needs {state.required} approval(s); "
        f"this level answers until {_utc(state.level_deadline)}; silence escalates, it never approves. "
        f"Approve: python -m incident_agent.hitl approve {state.id} --by <you> --confirm {fp}"
    )
    return Notification(kind, state.id, state.level, state.level_name, roles, text, level.get("mention"))


class OutboxNotifier:
    """Appends each notification to a JSONL file. The offline default and the audit trail of what was sent."""

    name = "outbox"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, note: Notification) -> NotifyResult:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(note.__dict__, sort_keys=True) + "\n")
                fh.flush()
        except OSError as exc:
            return NotifyResult(False, f"cannot write the outbox ({type(exc).__name__})")
        return NotifyResult(True, "written")


class SlackNotifier:
    """Posts through a Stage 15 sender (real webhook only when the caller built a ``WebhookSender``)."""

    name = "slack"

    def __init__(self, sender: Sender) -> None:
        self.sender = sender

    def send(self, note: Notification) -> NotifyResult:
        lead = f"{validate_mention(note.mention)} " if note.mention else ""
        body = slack_text(note.text, 1500)
        payload: dict[str, Any] = {"text": f"{lead}{body}"}
        result = self.sender.send(payload)
        return NotifyResult(result.ok, result.detail)


class CallbackNotifier:
    """Wraps a function (tests, pagers, anything else)."""

    name = "callback"

    def __init__(self, fn: Callable[[Notification], bool | None]) -> None:
        self._fn = fn

    def send(self, note: Notification) -> NotifyResult:
        ok = self._fn(note)
        return NotifyResult(ok is not False, "callback")


class MultiNotifier:
    """Every notifier must succeed for the delivery to count; a failing one is retried alone next tick."""

    name = "multi"

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        if not notifiers:
            raise ValueError("at least one notifier is needed")
        self.notifiers = tuple(notifiers)

    def send(self, note: Notification) -> NotifyResult:
        failures = []
        for n in self.notifiers:
            try:
                r = n.send(note)
            except Exception as exc:  # noqa: BLE001 - one broken channel must not hide the others
                r = NotifyResult(False, type(exc).__name__)
            if not r.ok:
                failures.append(f"{n.name}: {r.detail}")
        return NotifyResult(not failures, "; ".join(failures) or "delivered")
