"""Human-in-the-loop approval gate and escalation (Stage 18)."""

from incident_agent.hitl.bridge import Controller, approval_payload, request_id_for
from incident_agent.hitl.gate import (
    ApprovalGate,
    BadConfirmation,
    DuplicateApproval,
    GateError,
    NotEligible,
    NotPending,
    SelfApproval,
)
from incident_agent.hitl.notify import (
    CallbackNotifier,
    MultiNotifier,
    Notification,
    Notifier,
    NotifyResult,
    OutboxNotifier,
    SlackNotifier,
)
from incident_agent.hitl.policy import DEFAULT_POLICY, EscalationPolicy, Level, PolicyError, load_policy
from incident_agent.hitl.roster import Approver, AuthError, Roster
from incident_agent.hitl.store import RequestState, RequestStore

__all__ = [
    "DEFAULT_POLICY",
    "Approver",
    "ApprovalGate",
    "AuthError",
    "BadConfirmation",
    "CallbackNotifier",
    "Controller",
    "DuplicateApproval",
    "EscalationPolicy",
    "GateError",
    "Level",
    "MultiNotifier",
    "NotEligible",
    "NotPending",
    "Notification",
    "Notifier",
    "NotifyResult",
    "OutboxNotifier",
    "PolicyError",
    "RequestState",
    "RequestStore",
    "Roster",
    "SelfApproval",
    "SlackNotifier",
    "approval_payload",
    "load_policy",
    "request_id_for",
]
