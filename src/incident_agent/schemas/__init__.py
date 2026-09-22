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
    EvidenceType,
    IncidentMetadata,
    MetricAnomaly,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.schemas.command_diagnosis import CommanderDiagnosis
from incident_agent.schemas.diagnosis import Diagnosis

__all__ = [
    "AgentOutput",
    "CommanderDiagnosis",
    "Diagnosis",
    "EvidenceItem",
    "EvidenceType",
    "IncidentMetadata",
    "MetricAnomaly",
    "ReasoningStep",
    "RiskTier",
    "WorkerFinding",
]
