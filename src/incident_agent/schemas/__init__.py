"""Pydantic schemas for structured agent output.

Stage 4: Every agent output is a validated Pydantic object —
no free text passes between agents or to tools. These schemas
are the single source of truth for the shape of all inter-agent
and tool-call payloads, referenced by the OpenSpec proposal and
the agent responsibility matrix (§ Cross-cutting rules, rule #3).

Stage 9: Tree-of-Thought + Plan-and-Solve adds Hypothesis and
TreeOfThoughtResult schemas for parallel hypothesis branching.
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
from incident_agent.schemas.debate import Argument, DebateOutcome
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.schemas.tree_of_thought import (
    Hypothesis,
    TreeOfThoughtResult,
)

__all__ = [
    "AgentOutput",
    "Argument",
    "CommanderDiagnosis",
    "DebateOutcome",
    "Diagnosis",
    "EvidenceItem",
    "EvidenceType",
    "Hypothesis",
    "IncidentMetadata",
    "MetricAnomaly",
    "ReasoningStep",
    "RiskTier",
    "TreeOfThoughtResult",
    "WorkerFinding",
]
