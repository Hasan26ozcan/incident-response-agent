"""CommanderDiagnosis schema — the unified incident narrative from Stage 7.

Stage 7 (Hierarchical Swarm) adds an Incident Commander agent that
receives the orchestrator's Diagnosis and all worker findings, then
produces a CommanderDiagnosis containing a unified incident narrative,
an escalation decision, and a severity assessment.

The CommanderDiagnosis extends AgentOutput with narrative-level
fields that sit above the technical Diagnosis produced by the
OrchestratorAgent. All fields are validated by Pydantic.

Cross-cutting rule (Stage 4): every agent output is a validated
Pydantic object.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from incident_agent.schemas.agent_output import (
    AgentOutput,
    EvidenceItem,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.schemas.diagnosis import Diagnosis


class CommanderDiagnosis(AgentOutput):
    """Unified incident narrative produced by the Incident Commander.

    The Incident Commander receives the OrchestratorAgent's Diagnosis
    and all specialist worker findings, then synthesizes them into
    a higher-level incident narrative with an escalation decision.

    This sits above the technical Diagnosis — it adds the
    "so what" layer: what this incident means for the business,
    whether to escalate, and how severe the situation is.

    All fields are validated by Pydantic at construction time.
    """

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident ID")
    narrative: str = Field(min_length=20, description="Unified incident narrative")
    escalation_decision: str = Field(
        min_length=1,
        description="Escalation decision: 'escalate', 'monitor', or 'resolve'",
    )
    severity_assessment: str = Field(
        min_length=10,
        description="Overall severity assessment of the incident",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Commander confidence in the narrative")
    risk_tier: RiskTier = Field(description="Risk classification inherited from diagnosis")
    diagnosis: Diagnosis = Field(description="The underlying orchestrator diagnosis")
    worker_findings: list[WorkerFinding] = Field(
        min_length=1,
        description="All worker findings that informed this narrative",
    )
    evidence: list[EvidenceItem] = Field(min_length=1, description="Aggregated evidence")
    affected_service: str = Field(min_length=1, description="Name of the affected service")
    category: str = Field(min_length=1, description="Incident category")
    recommendation: str = Field(min_length=10, description="Commander-level recommendation")
    reasoning_steps: list[ReasoningStep] = Field(min_length=1, description="Commander-level reasoning steps")

    @field_validator("narrative")
    @classmethod
    def narrative_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 20:
            raise ValueError("narrative must be at least 20 characters")
        return cleaned

    @field_validator("escalation_decision")
    @classmethod
    def escalation_decision_must_be_valid(cls, v: str) -> str:
        cleaned = v.strip().lower()
        valid = ("escalate", "monitor", "resolve")
        if cleaned not in valid:
            raise ValueError(f"escalation_decision must be one of {valid}")
        return cleaned

    @field_validator("severity_assessment")
    @classmethod
    def severity_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 10:
            raise ValueError("severity_assessment must be at least 10 characters")
        return cleaned

    def format_report(self) -> str:
        """Return a human-readable commander report."""
        risk_label = self.risk_tier.value if isinstance(self.risk_tier, RiskTier) else self.risk_tier
        lines = [
            f"=== Incident Commander Report: {self.incident_id} ===",
            f"Service: {self.affected_service}",
            f"Category: {self.category}",
            f"Risk Tier: {risk_label}",
            f"Severity: {self.severity_assessment}",
            f"Escalation: {self.escalation_decision}",
            f"Confidence: {self.confidence:.0%}",
            "",
            "Narrative:",
            f"  {self.narrative}",
            "",
            "Evidence:",
        ]
        for item in self.evidence:
            lines.append(f"  - [{item.source_type.value}] {item.detail}")
        lines.append("")
        lines.append("Reasoning:")
        for i, step in enumerate(self.reasoning_steps, 1):
            description = step.description if hasattr(step, "description") else str(step)
            lines.append(f"  {i}. {description}")
        lines.append("")
        lines.append(f"Recommendation: {self.recommendation}")
        lines.append("")
        lines.append(f"Underlying Diagnosis (confidence {self.diagnosis.confidence:.0%}):")
        lines.append(f"  Root Cause: {self.diagnosis.root_cause}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Return JSON serialization of the commander diagnosis."""
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str) -> CommanderDiagnosis:
        """Parse a CommanderDiagnosis from a JSON string."""
        return cls.model_validate_json(data)

    def model_dump(self, *args, **kwargs) -> dict:
        """Return a dict representation, converting Enums to values."""
        data = super().model_dump(*args, **kwargs)
        if isinstance(data.get("risk_tier"), RiskTier):
            data["risk_tier"] = self.risk_tier.value
        return data
