"""Stage 20: adversarial testing and security (signed approvals, kill-switch, injection handling, attack report)."""

from incident_agent.security.signing import ApprovalSigner, SigningError, load_or_create_key

__all__ = ["ApprovalSigner", "SigningError", "load_or_create_key"]
