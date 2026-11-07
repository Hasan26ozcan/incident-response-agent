"""Sandboxed execution of risky commands (Stage 16): allow-list, isolation backends, approvals, audit, escape tests."""

from incident_agent.sandbox.approval import Approval, issue_approval
from incident_agent.sandbox.audit import AuditLog
from incident_agent.sandbox.backends import Backend, Limits, select_backend
from incident_agent.sandbox.policy import Decision, evaluate
from incident_agent.sandbox.runner import RunResult, SandboxRunner

__all__ = [
    "Approval",
    "AuditLog",
    "Backend",
    "Decision",
    "Limits",
    "RunResult",
    "SandboxRunner",
    "evaluate",
    "issue_approval",
    "select_backend",
]
