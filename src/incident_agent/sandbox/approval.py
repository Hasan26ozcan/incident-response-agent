"""Approvals for high-risk commands (Stage 16 interface; the human gate itself is Stage 18).

An approval is bound to one fingerprint (incident + command + normalised arguments), has a short lifetime and can be
used once. This module only *verifies* an approval a human issued; who may issue one, how they are asked and what
happens on timeout (escalate, never auto-approve) is Stage 18. ``issue_approval`` exists for that stage's gate, for
tests and for the local CLI stand-in, which is a demonstration, not a security boundary against an agent that can run
the CLI itself.
"""

from __future__ import annotations

import hmac
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

MAX_TTL = 3600.0
CLOCK_SKEW = 60.0
FINGERPRINT_RE = re.compile(r"[0-9a-f]{20}")


@dataclass(frozen=True)
class Approval:
    fingerprint: str
    approved_by: str
    issued_at: float
    expires_at: float
    nonce: str


def issue_approval(
    fingerprint: str, approved_by: str, *, ttl: float = 300.0, clock: Callable[[], float] = time.time
) -> Approval:
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("fingerprint must be 20 lowercase hex characters")
    if not approved_by.strip() or len(approved_by) > 80 or not approved_by.isprintable():
        raise ValueError("approved_by must name a person (1 to 80 printable characters)")
    if not 0 < ttl <= MAX_TTL:
        raise ValueError(f"ttl must be between 0 and {MAX_TTL:g} seconds")
    now = clock()
    return Approval(fingerprint, approved_by.strip(), now, now + ttl, uuid.uuid4().hex)


def check_approval(
    approval: Approval | None, fingerprint: str, *, now: float, is_used: Callable[[str], bool]
) -> str | None:
    """Return why the approval is not acceptable, or None when it is."""
    if approval is None:
        return "no approval was given"
    if not isinstance(approval, Approval):
        return "approval is not an Approval"
    if not hmac.compare_digest(approval.fingerprint, fingerprint):
        return "the approval is for a different command"
    if not approval.approved_by.strip():
        return "the approval does not name an approver"
    if approval.issued_at > now + CLOCK_SKEW:
        return "the approval is dated in the future"
    if approval.expires_at <= approval.issued_at or approval.expires_at - approval.issued_at > MAX_TTL:
        return "the approval has an invalid lifetime"
    if now >= approval.expires_at:
        return "the approval has expired"
    if not approval.nonce or is_used(approval.nonce):
        return "the approval was already used"
    return None
