"""Pydantic schemas for structured agent output.

Stage 4: Every agent output is a validated Pydantic object —
no free text passes between agents or to tools. These schemas
are the single source of truth for the shape of all inter-agent
and tool-call payloads, referenced by the OpenSpec proposal and
the agent responsibility matrix (§ Cross-cutting rules, rule #3).
"""

from __future__ import annotations

from incident_agent.schemas.agent_output import (
    AgentOutput,
    EvidenceItem,
    IncidentMetadata,
    MetricAnomaly,
    ReasoningStep,
    RiskTier,
)
from incident_agent.schemas.diagnosis import Diagnosis

__all__ = [
    "AgentOutput",
    "Diagnosis",
    "EvidenceItem",
    "IncidentMetadata",
    "MetricAnomaly",
    "ReasoningStep",
    "RiskTier",
]
