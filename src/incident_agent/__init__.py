"""Incident Response Agent — agentic incident triage and diagnosis.

This package is built up incrementally across the 23-stage roadmap in
ROADMAP.md. At Stage 4, all agent outputs are Pydantic-validated
objects with a prompt library supporting few-shot examples and
structured system prompts.

See:
  - incident_agent.schemas — Pydantic output schemas (Stage 4)
  - incident_agent.prompts — Prompt library (Stage 4)
  - incident_agent.agents.react_agent — ReAct agent (Stage 3→4)
"""

__version__ = "0.1.0"

from incident_agent.agents.react_agent import Diagnosis, ReActAgent
from incident_agent.schemas import (
    AgentOutput,
    EvidenceItem,
    IncidentMetadata,
    MetricAnomaly,
    ReasoningStep,
    RiskTier,
)

__all__ = [
    "__version__",
    "Diagnosis",
    "ReActAgent",
    "AgentOutput",
    "EvidenceItem",
    "IncidentMetadata",
    "MetricAnomaly",
    "ReasoningStep",
    "RiskTier",
]
