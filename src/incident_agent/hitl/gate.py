"""The approval gate (Stage 18).

``ApprovalGate`` owns the life of an approval request:

    open -> pending(level 0) -> [timeout: escalate to level 1 ... last level] -> expired
                  |-> enough distinct, authenticated, eligible approvals -> approved
                  |-> one eligible rejection                              -> rejected
                  |-> the run no longer waits                             -> cancelled

Rules (``openspec/risk-classification.md``, high risk):

* execution stays blocked until a human explicitly approves;
* a timeout escalates, it never approves: the last level running out *expires* the request, and an expired request is
  a refusal;
* an approval names the exact action: the approver must type the first characters of its fingerprint.

On top of that: approvers authenticate with a token, must hold a role asked so far, cannot count twice, cannot approve
their own request, and a late approval never revives a request that already timed out (timeouts are applied before
every decision). Everything is appended to the request's journal; refused attempts are recorded too.
"""

from __future__ import annotations

import hmac
import re
import time
from collections.abc import Callable
from typing import Any

from incident_agent.durable.journal import LockError
from incident_agent.hitl.notify import CONFIRM_LEN, Notifier, NotifyResult, build_notification
from incident_agent.hitl.policy import EscalationPolicy
from incident_agent.hitl.roster import AuthError, Roster
from incident_agent.hitl.store import MAX_DENIED_RECORDS, RequestState, RequestStore, StoreTampered, check_request_id
from incident_agent.reporting.sanitize import one_line

FINGERPRINT_RE = re.compile(r"[0-9a-f]{20}")
MAX_NOTIFY_ATTEMPTS = 5  # per kind and level; after that the state shows the delivery as failed


class GateError(Exception):
    """A decision was refused. The request is unchanged apart from a log line."""


class NotPending(GateError):
    pass


class NotEligible(GateError):
    pass


class BadConfirmation(GateError):
    pass


class DuplicateApproval(GateError):
    pass


class SelfApproval(GateError):
    pass


class ApprovalGate:
    def __init__(
        self,
        store: RequestStore,
        roster: Roster,
        policy: EscalationPolicy,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store, self.roster, self.policy, self.notifier, self.clock = store, roster, policy, notifier, clock

    # -- opening ------------------------------------------------------------------------------------------------

    def open(
        self,
        request_id: str,
        *,
        run_id: str,
        incident_id: str,
        command_line: str,
        fingerprint: str,
        tier: str,
        reason: str,
        requested_by: str | None = None,
        hard_deadline: float | None = None,
    ) -> RequestState:
        """Open a request (idempotent: the same id and content returns the existing one)."""
        check_request_id(request_id)
        if tier == "low":
            raise ValueError("low-risk actions need no approval")
        if not FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError("fingerprint must be 20 lowercase hex characters")
        if not command_line.strip() or len(command_line) > 500:
            raise ValueError("command line must be 1-500 characters")
        with self.store.locked(request_id):
            if self.store.exists(request_id):
                st = self.store.state(request_id)
                if (st.run_id, st.fingerprint, st.incident_id) != (run_id, fingerprint, incident_id):
                    raise ValueError(f"request {request_id} already exists for a different action")
                return st
            now = self.clock()
            if hard_deadline is not None and hard_deadline <= now:
                raise ValueError("the run's wait deadline has already passed")
            self.store.append(
                request_id,
                "request_opened",
                run_id=run_id,
                incident_id=incident_id,
                command_line=command_line,
                fingerprint=fingerprint,
                tier=tier,
                reason=one_line(reason, 300),
                requested_by=requested_by,
                opened_at=now,
                required=self.policy.required_for(tier),
                levels=self.policy.to_json()["levels"],
                deadlines=self.policy.deadlines(now, hard_deadline),
                hard_deadline=hard_deadline,
            )
            self._notify_locked(request_id)
            return self.store.state(request_id)

    # -- deciding -----------------------------------------------------------------------------------------------

    def approve(self, request_id: str, name: str, token: str, *, confirm: str, comment: str = "") -> RequestState:
        return self._decide(request_id, name, token, True, confirm, comment)

    def reject(self, request_id: str, name: str, token: str, *, comment: str = "") -> RequestState:
        return self._decide(request_id, name, token, False, None, comment)

    def _deny(self, request_id: str, name: str, why: str) -> None:
        if self.store.state(request_id).denied < MAX_DENIED_RECORDS:
            self.store.append(request_id, "attempt_denied", who=one_line(name, 40), why=why, at=self.clock())

    def _decide(
        self, request_id: str, name: str, token: str, approve: bool, confirm: str | None, comment: str
    ) -> RequestState:
        with self.store.locked(request_id):
            self._advance_locked(request_id)  # a timeout that already happened wins over a late decision
            st = self.store.state(request_id)
            if not st.pending:
                self._notify_locked(request_id)
                raise NotPending(f"request {request_id} is already {st.outcome}")
            try:
                who = self.roster.authenticate(name, token)
            except AuthError:
                self._deny(request_id, name, "authentication failed")
                raise
            if st.requested_by is not None and who.name == st.requested_by:
                self._deny(request_id, who.name, "requester cannot decide their own request")
                raise SelfApproval("you opened this request, someone else must decide it")
            if not who.roles & st.roles_asked():
                self._deny(request_id, who.name, "role not asked at this level")
                raise NotEligible(
                    f"{who.name} holds none of the roles asked so far ({', '.join(sorted(st.roles_asked()))})"
                )
            note = one_line(comment, 300)
            now = self.clock()
            if approve:
                if confirm is None or not hmac.compare_digest(confirm.strip().lower(), st.fingerprint[:CONFIRM_LEN]):
                    self._deny(request_id, who.name, "fingerprint confirmation did not match")
                    raise BadConfirmation(
                        f"--confirm must be the first {CONFIRM_LEN} characters of the action's fingerprint"
                    )
                if who.name in {a["by"] for a in st.approvals}:
                    raise DuplicateApproval(f"{who.name} already approved this request")
                self.store.append(request_id, "approved", by=who.name, at=now, comment=note)
                if len(st.approvals) + 1 >= st.required:
                    names = [a["by"] for a in st.approvals] + [who.name]
                    self._resolve(request_id, "approved", f"{st.required} approval(s) received", names, now)
            else:
                self.store.append(request_id, "rejected", by=who.name, at=now, comment=note)
                self._resolve(request_id, "rejected", f"rejected by {who.name}", [who.name], now)
            self._notify_locked(request_id)
            return self.store.state(request_id)

    def _resolve(self, request_id: str, outcome: str, reason: str, by: list[str], now: float) -> None:
        self.store.append(request_id, "resolved", outcome=outcome, reason=reason, by=by, at=now)

    # -- time ---------------------------------------------------------------------------------------------------

    def _advance_locked(self, request_id: str) -> None:
        """Apply every timeout that has passed: escalate level by level, expire after the last one."""
        while True:
            st = self.store.state(request_id)
            now = self.clock()
            if not st.pending or now < st.level_deadline:
                return
            if st.is_last_level:
                self._resolve(request_id, "expired", "no approval before the last escalation level ran out", [], now)
                return
            self.store.append(
                request_id, "escalated", **{"from": st.level, "to": st.level + 1, "at": now}, why="timeout"
            )

    def _notify_locked(self, request_id: str) -> None:
        st = self.store.state(request_id)
        if st.pending:
            kind = "opened" if st.level == 0 else "escalated"
        else:
            kind = "resolved"
        if st.notified(kind, st.level) or st.notify_attempts(kind, st.level) >= MAX_NOTIFY_ATTEMPTS:
            return
        note = build_notification(st, kind)
        try:
            result = self.notifier.send(note)
        except Exception as exc:  # noqa: BLE001 - a broken channel is a recorded failure, not a lost request
            result = NotifyResult(False, type(exc).__name__)
        self.store.append(
            request_id,
            "notification",
            topic=kind,
            level=st.level,
            ok=result.ok,
            detail=one_line(result.detail, 160),
            at=self.clock(),
        )

    def tick(self) -> list[dict[str, Any]]:
        """Escalate or expire what is due and retry what could not be delivered. Safe to call from many processes."""
        out: list[dict[str, Any]] = []
        for rid in self.store.ids():
            try:
                with self.store.locked(rid, wait=2.0):
                    before = self.store.state(rid)
                    if before.pending or not before.notified("resolved", before.level):
                        self._advance_locked(rid)
                        self._notify_locked(rid)
                    after = self.store.state(rid)
            except (LockError, StoreTampered):
                continue  # a tampered request is never advanced or delivered; its run expires closed
            if (after.level, after.status, after.events) != (before.level, before.status, before.events):
                out.append({"id": rid, "status": after.status, "level": after.level_name})
        return out

    def cancel(self, request_id: str, reason: str) -> RequestState:
        with self.store.locked(request_id):
            self._advance_locked(request_id)
            st = self.store.state(request_id)
            if st.pending:
                self._resolve(request_id, "cancelled", one_line(reason, 160), [], self.clock())
                self._notify_locked(request_id)
            return self.store.state(request_id)

    def mark_delivered(self, request_id: str) -> None:
        with self.store.locked(request_id):
            if not self.store.state(request_id).delivered:
                self.store.append(request_id, "delivered", at=self.clock())
