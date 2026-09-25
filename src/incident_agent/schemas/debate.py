"""Debate mechanism schemas — Stage 8.

Stage 8 introduces the debate mechanism where a root-cause agent
argues for an initial diagnosis and a forensic examiner agent
challenges it. The debate produces a DebateOutcome containing
the before/after false-positive rate comparison and a final verdict.

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from incident_agent.schemas.agent_output import (
    AgentOutput,
    ReasoningStep,
)
from incident_agent.schemas.diagnosis import Diagnosis


class Argument(BaseModel):
    """A single debate argument produced by an agent.

    Represents one point made by either the root-cause agent
    (supporting the diagnosis) or the forensic examiner agent
    (challenging the diagnosis).
    """

    agent: str = Field(
        min_length=1,
        description="Agent that produced this argument: 'root_cause' or 'forensic_examiner'",
    )
    point: str = Field(min_length=5, description="The argument point")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="References to evidence supporting this argument",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this argument (0.0-1.0)",
    )

    @field_validator("point")
    @classmethod
    def point_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 5:
            raise ValueError("point must be at least 5 characters")
        return cleaned


class DebateOutcome(AgentOutput):
    """The result of the root-cause vs forensic examiner debate.

    Stage 8: The debate mechanism compares false-positive rates
    before and after the debate, producing a verdict on the
    initial diagnosis.

    All fields are validated by Pydantic.
    """

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident ID")
    original_diagnosis: Diagnosis = Field(description="The initial diagnosis before debate")
    root_cause_arguments: list[Argument] = Field(
        min_length=1,
        description="Arguments supporting the root cause diagnosis",
    )
    forensic_challenges: list[Argument] = Field(
        min_length=1,
        description="Challenges raised by the forensic examiner",
    )
    verdict: str = Field(
        min_length=1,
        description="Verdict: 'confirmed', 'challenged', or 'revised'",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Debate confidence after considering challenges",
    )
    false_positive_rate_before: float = Field(
        ge=0.0,
        le=1.0,
        description="False-positive rate before the debate",
    )
    false_positive_rate_after: float = Field(
        ge=0.0,
        le=1.0,
        description="False-positive rate after the debate",
    )
    confidence_adjustment: float = Field(
        description="Net confidence change from the debate (positive or negative)",
    )
    final_diagnosis: Diagnosis = Field(description="The final diagnosis after debate")
    debate_rounds: int = Field(ge=1, description="Number of debate rounds exchanged")
    reasoning_steps: list[ReasoningStep] = Field(
        min_length=1,
        description="Debate-level reasoning steps",
    )
    recommendation: str = Field(min_length=10, description="Debate-driven recommendation")

    @field_validator("verdict")
    @classmethod
    def verdict_must_be_valid(cls, v: str) -> str:
        cleaned = v.strip().lower()
        valid = ("confirmed", "challenged", "revised")
        if cleaned not in valid:
            raise ValueError(f"verdict must be one of {valid}")
        return cleaned

    @field_validator("false_positive_rate_after")
    @classmethod
    def fpr_must_be_valid(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("false_positive_rate_after must be between 0.0 and 1.0")
        return v

    def format_report(self) -> str:
        """Return a human-readable debate report."""
        lines = [
            f"=== Debate Outcome: {self.incident_id} ===",
            f"Verdict: {self.verdict}",
            f"Debate Rounds: {self.debate_rounds}",
            "",
            "False-Positive Rate Comparison:",
            f"  Before Debate: {self.false_positive_rate_before:.2%}",
            f"  After Debate:  {self.false_positive_rate_after:.2%}",
            f"  Improvement:   {(self.false_positive_rate_before - self.false_positive_rate_after):.2%}",
            "",
            f"Confidence Adjustment: {self.confidence_adjustment:+.2f}",
            "",
            "Root-Cause Arguments:",
        ]
        for arg in self.root_cause_arguments:
            lines.append(f"  [{arg.agent}] ({arg.confidence:.0%}) {arg.point}")
        lines.append("")
        lines.append("Forensic Challenges:")
        for chal in self.forensic_challenges:
            lines.append(f"  [{chal.agent}] ({chal.confidence:.0%}) {chal.point}")
        lines.append("")
        lines.append(f"Recommendation: {self.recommendation}")
        lines.append("")
        lines.append(f"Final Diagnosis (confidence {self.final_diagnosis.confidence:.0%}):")
        lines.append(f"  Root Cause: {self.final_diagnosis.root_cause}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Return JSON serialization of the debate outcome."""
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str) -> DebateOutcome:
        """Parse a DebateOutcome from a JSON string."""
        return cls.model_validate_json(data)

    def model_dump(self, *args, **kwargs) -> dict:
        """Return a dict representation."""
        return super().model_dump(*args, **kwargs)
