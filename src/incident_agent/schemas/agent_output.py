"""Shared Pydantic schemas for all agent outputs.

Every agent in the system produces output conforming to these
models. They enforce structure, types, and value constraints at
the schema level so no agent ever passes free text to another
agent or to a tool.

Cross-cutting rule (see openspec/agent-responsibility-matrix.md):
    "Every agent's output is a validated Pydantic object (Stage 4
    onward)."
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class EvidenceType(str, Enum):
    """Category of evidence produced during diagnosis."""

    LOG_ENTRY = "log_entry"
    METRIC_ANOMALY = "metric_anomaly"
    DEPLOY = "deploy"
    META = "meta"


class RiskTier(str, Enum):
    """Risk classification for an incident."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceItem(BaseModel):
    """A single piece of evidence supporting the diagnosis."""

    source_type: EvidenceType
    source: str
    detail: str
    timestamp: str = Field(default="")
    confidence_weight: float = Field(default=1.0, ge=0.0, le=1.0)

    def __contains__(self, item: str) -> bool:
        """Support `item in evidence_item` checks against the detail field."""
        return item in self.detail

    @field_validator("source")
    @classmethod
    def source_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source must not be empty")
        return v


class IncidentMetadata(BaseModel):
    """Parsed incident metadata from meta.json."""

    incident_id: str = Field(pattern=r"^INC-\d{3}$")
    category: str
    services: list[str] = Field(min_length=1)
    window_start: str
    window_end: str
    severity: str = "unknown"
    additional_info: dict = Field(default_factory=dict)

    @field_validator("category")
    @classmethod
    def category_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("category must not be empty")
        return v


class MetricAnomaly(BaseModel):
    """A detected anomaly in a metric time series."""

    metric_name: str
    timestamp: str
    value: float
    baseline_avg: float = Field(ge=0)
    baseline_stdev: float = Field(ge=0)
    threshold: float = Field(ge=0)

    @field_validator("value")
    @classmethod
    def value_must_be_numeric(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise ValueError("value must be numeric")
        return float(v)


class ReasoningStep(BaseModel):
    """A single step in the agent's reasoning chain."""

    step_number: int = Field(ge=1)
    description: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def description_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be empty")
        return v.strip()


class AgentOutput(BaseModel):
    """Base interface for all agent outputs.

    Every agent output MUST inherit from this model to ensure
    structural consistency across the multi-agent system.
    """

    schema_version: str = Field(default="1.0")
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    agent_type: str = Field(...)
    incident_id: str = Field(pattern=r"^INC-\d{3}$")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("agent_type")
    @classmethod
    def agent_type_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("agent_type must not be empty")
        return v.strip()
