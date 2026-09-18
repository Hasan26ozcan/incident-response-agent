"""Diagnosis schema — the primary structured output of the incident diagnosis agent.

Stage 4 replaces the dataclasses.Diagnosis from Stage 3 with this
Pydantic model. It enforces:
  - All fields are typed and validated at construction time
  - JSON serialization is deterministic and schema-compliant
  - Confidence is bounded [0.0, 1.0]
  - Risk tier is constrained to the RiskTier enum
  - Evidence and reasoning steps are non-empty validated collections

See openspec/agent-responsibility-matrix.md § Cross-cutting rules
(rule #3): every agent's output is a validated Pydantic object.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from incident_agent.schemas.agent_output import (
    AgentOutput,
    EvidenceItem,
    IncidentMetadata,
    ReasoningStep,
    RiskTier,
)


class Diagnosis(AgentOutput):
    """Structured diagnosis output for an incident.

    Inherits from AgentOutput, adding diagnosis-specific fields.
    All fields are validated by Pydantic at construction time.
    """

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident ID")
    root_cause: str = Field(min_length=10, description="Root cause description, at least 10 chars")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    evidence: list[EvidenceItem] = Field(min_length=1, description="At least one evidence item required")
    affected_service: str = Field(min_length=1, description="Name of the affected service")
    category: str = Field(min_length=1, description="Incident category")
    reasoning_steps: list[ReasoningStep] = Field(min_length=1, description="At least one reasoning step required")
    recommendation: str = Field(min_length=10, description="Remediation recommendation, at least 10 chars")
    risk_tier: RiskTier = Field(description="Risk classification for this incident")
    metadata: IncidentMetadata | None = Field(default=None, description="Optional parsed incident metadata")

    @field_validator("root_cause")
    @classmethod
    def root_cause_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 10:
            raise ValueError("root_cause must be at least 10 characters")
        return cleaned

    @field_validator("recommendation")
    @classmethod
    def recommendation_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 10:
            raise ValueError("recommendation must be at least 10 characters")
        return cleaned

    @field_validator("affected_service")
    @classmethod
    def affected_service_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("affected_service must not be empty")
        return cleaned

    @field_validator("category")
    @classmethod
    def category_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("category must not be empty")
        return cleaned

    def format_report(self) -> str:
        """Return a human-readable diagnosis report.

        Preserves the Stage 3 format_report() API for backward
        compatibility with CLI and existing tests.
        """
        risk_label = self.risk_tier.value if isinstance(self.risk_tier, RiskTier) else self.risk_tier
        lines = [
            f"=== Diagnosis: {self.incident_id} ===",
            f"Service: {self.affected_service}",
            f"Category: {self.category}",
            f"Root Cause: {self.root_cause}",
            f"Confidence: {self.confidence:.0%}",
            f"Risk Tier: {risk_label}",
            "",
            "Evidence:",
        ]
        for item in self.evidence:
            lines.append(f"  - [{item.source_type.value}] {item.detail}")
        lines.append("")
        lines.append("Reasoning:")
        for i, step in enumerate(self.reasoning_steps, 1):
            lines.append(f"  {i}. {step.description}")
        lines.append("")
        lines.append(f"Recommendation: {self.recommendation}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Return JSON serialization of the diagnosis.

        Uses Pydantic's model_dump_json for deterministic output
        compatible with JSON-mode LLM grammars.
        """
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str) -> Diagnosis:
        """Parse a Diagnosis from a JSON string.

        Validates the JSON against the schema, raising
        ValidationError if the data does not conform.
        """
        return cls.model_validate_json(data)

    def model_dump(self, *args, **kwargs) -> dict:
        """Return a dict representation, converting Enums to values.

        Useful for LLM tool-call payloads where enum values must
        be serialized as strings.
        """
        data = super().model_dump(*args, **kwargs)
        if isinstance(data.get("risk_tier"), RiskTier):
            data["risk_tier"] = self.risk_tier.value
        return data
