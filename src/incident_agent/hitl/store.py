"""Approval requests as an event-sourced store (Stage 18).

One directory per request holding a ``journal.jsonl`` (the same fsynced, torn-tail-safe journal the durable engine
uses). The request's state is a fold of its events, so a crash anywhere, a second controller process, or a restart on
another day all see the same truth. Nothing is ever edited or deleted: a decision, an escalation and every refused
attempt stay in the log.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incident_agent.durable.journal import Journal, LockError, RunLock

REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
MAX_DENIED_RECORDS = 50  # refused attempts are logged, but a flood cannot grow the journal without bound
TERMINAL = frozenset({"approved", "rejected", "expired", "cancelled"})


def check_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id) or request_id.endswith("."):
        raise ValueError("request id must be 1-80 characters of letters, digits, '.', '_' or '-'")
    return request_id


@dataclass(frozen=True)
class RequestState:
    id: str
    run_id: str
    incident_id: str
    command_line: str
    fingerprint: str
    tier: str
    reason: str
    requested_by: str | None
    opened_at: float
    required: int
    levels: tuple[dict[str, Any], ...]
    deadlines: tuple[float, ...]
    hard_deadline: float | None
    level: int
    approvals: tuple[dict[str, Any], ...]
    outcome: str | None
    resolved_at: float | None
    resolved_reason: str
    decided_by: tuple[str, ...]
    notifications: tuple[dict[str, Any], ...]
    delivered: bool
    denied: int
    events: int

    @property
    def status(self) -> str:
        return self.outcome or "pending"

    @property
    def pending(self) -> bool:
        return self.outcome is None

    @property
    def level_name(self) -> str:
        return str(self.levels[self.level]["name"])

    @property
    def level_deadline(self) -> float:
        return self.deadlines[self.level]

    @property
    def is_last_level(self) -> bool:
        return self.level >= len(self.levels) - 1 or (
            self.hard_deadline is not None and self.deadlines[self.level] >= self.hard_deadline
        )

    def roles_asked(self) -> frozenset[str]:
        return frozenset(str(r) for lv in self.levels[: self.level + 1] for r in lv["roles"])

    def notify_attempts(self, kind: str, level: int) -> int:
        return sum(1 for n in self.notifications if n["topic"] == kind and n["level"] == level)

    def notified(self, kind: str, level: int) -> bool:
        return any(n["topic"] == kind and n["level"] == level and n["ok"] for n in self.notifications)


def fold(request_id: str, events: list[dict[str, Any]]) -> RequestState:
    if not events or events[0]["kind"] != "request_opened":
        raise ValueError(f"request {request_id} has no opening event")
    o = events[0]
    level, outcome, resolved_at, reason = 0, None, None, ""
    approvals: list[dict[str, Any]] = []
    decided: tuple[str, ...] = ()
    notes: list[dict[str, Any]] = []
    delivered, denied = False, 0
    for e in events[1:]:
        k = e["kind"]
        if k == "escalated":
            level = int(e["to"])
        elif k == "approved":
            approvals.append({"by": e["by"], "at": e["at"], "comment": e.get("comment", "")})
        elif k == "resolved":
            outcome, resolved_at, reason = e["outcome"], e["at"], e.get("reason", "")
            decided = tuple(e.get("by", ()))
        elif k == "notification":
            notes.append({key: e[key] for key in ("topic", "level", "ok", "detail", "at")})
        elif k == "delivered":
            delivered = True
        elif k == "attempt_denied":
            denied += 1
    return RequestState(
        id=request_id,
        run_id=o["run_id"],
        incident_id=o["incident_id"],
        command_line=o["command_line"],
        fingerprint=o["fingerprint"],
        tier=o["tier"],
        reason=o["reason"],
        requested_by=o.get("requested_by"),
        opened_at=o["opened_at"],
        required=int(o["required"]),
        levels=tuple(o["levels"]),
        deadlines=tuple(o["deadlines"]),
        hard_deadline=o.get("hard_deadline"),
        level=level,
        approvals=tuple(approvals),
        outcome=outcome,
        resolved_at=resolved_at,
        resolved_reason=reason,
        decided_by=decided,
        notifications=tuple(notes),
        delivered=delivered,
        denied=denied,
        events=len(events),
    )


class RequestStore:
    def __init__(self, root: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.clock = clock

    def dir(self, request_id: str) -> Path:
        return self.root / check_request_id(request_id)

    def journal(self, request_id: str) -> Journal:
        return Journal(self.dir(request_id), self.clock)

    def exists(self, request_id: str) -> bool:
        return self.journal(request_id).exists()

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if (p / "journal.jsonl").exists() and REQUEST_ID_RE.fullmatch(p.name)
        )

    def state(self, request_id: str) -> RequestState:
        j = self.journal(request_id)
        if not j.exists():
            raise KeyError(f"unknown request {request_id!r}")
        return fold(request_id, j.events())

    def append(self, request_id: str, kind: str, **fields: Any) -> dict[str, Any]:
        return self.journal(request_id).append(kind, **fields)

    @contextmanager
    def locked(self, request_id: str, wait: float = 30.0) -> Iterator[None]:
        """Exclusive access to one request. Waits for another process to finish, then gives up."""
        lock = RunLock(self.dir(request_id))
        give_up = time.monotonic() + wait
        while True:
            try:
                lock.acquire()
                break
            except LockError:
                if time.monotonic() >= give_up:
                    raise LockError(f"request {request_id} is busy (another process is working on it)") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.release()
