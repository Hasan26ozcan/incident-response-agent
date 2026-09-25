"""Tree-of-Thought + Plan-and-Solve schemas — Stage 9.

Stage 9 introduces parallel hypothesis branching: multiple alternative
root-cause explanations are generated, independently scored against
the evidence, and the most likely scenario is selected. A Plan-and-Solve
step validates the selected hypothesis before finalising the result.

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from incident_agent.schemas.agent_output import (
    AgentOutput,
    EvidenceItem,
    ReasoningStep,
    RiskTier,
)
from incident_agent.schemas.diagnosis import Diagnosis


class Hypothesis(BaseModel):
    """A single root-cause hypothesis generated during Tree-of-Thought reasoning.

    Each hypothesis represents one possible explanation for the incident.
    Hypotheses are generated in parallel, scored, and the most likely
    one is selected for further validation.
    """

    hypothesis_id: str = Field(min_length=1, description="Unique identifier for this hypothesis")
    scenario: str = Field(min_length=10, description="Description of the hypothesized scenario")
    root_cause: str = Field(min_length=5, description="The hypothesized root cause")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score for this hypothesis (0.0-1.0)")
    supporting_evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="Evidence items that support this hypothesis",
    )
    reasoning_steps: list[ReasoningStep] = Field(
        default_factory=list,
        description="Reasoning steps that led to this hypothesis",
    )
    category: str = Field(min_length=1, description="Incident category")
    risk_tier: RiskTier = Field(description="Risk tier associated with this hypothesis")

    @field_validator("scenario")
    @classmethod
    def scenario_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 10:
            raise ValueError("scenario must be at least 10 characters")
        return cleaned

    @field_validator("root_cause")
    @classmethod
    def root_cause_must_not_be_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 5:
            raise ValueError("root_cause must be at least 5 characters")
        return cleaned


class TreeOfThoughtResult(AgentOutput):
    """The result of the Tree-of-Thought + Plan-and-Solve process.

    Stage 9: Multiple hypotheses are generated, scored, and pruned.
    The best hypothesis is validated via Plan-and-Solve before producing
    the final diagnosis. This produces measurable accuracy improvement
    on the gold set by selecting the most evidence-supported scenario.

    All fields are validated by Pydantic.
    """

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident ID")
    hypotheses: list[Hypothesis] = Field(
        min_length=2,
        description="All generated hypotheses (at least 2 for branching)",
    )
    selected_hypothesis: Hypothesis = Field(
        description="The best hypothesis selected after scoring",
    )
    selected_hypothesis_index: int = Field(
        ge=0,
        description="Index of the selected hypothesis in the hypotheses list",
    )
    reasoning_steps: list[ReasoningStep] = Field(
        min_length=1,
        description="Tree-of-Thought reasoning steps documenting the branching process",
    )
    selected_evidence: list[EvidenceItem] = Field(
        min_length=1,
        description="Evidence that supports the selected hypothesis",
    )
    plan_and_solve_validation: str = Field(
        min_length=10,
        description="Description of the Plan-and-Solve validation performed",
    )
    accuracy_improvement: float = Field(
        ge=0.0,
        le=1.0,
        description="Measured accuracy improvement from using ToT (0.0-1.0)",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Final confidence after ToT selection and validation",
    )
    risk_tier: RiskTier = Field(description="Risk tier inherited from diagnosis")
    final_diagnosis: Diagnosis = Field(description="The final Diagnosis after ToT selection")
    recommendation: str = Field(min_length=10, description="Recommendation based on selected hypothesis")
    hypothesis_scores: dict[str, float] = Field(
        description="Mapping of hypothesis_id to its evaluation score",
    )

    @field_validator("accuracy_improvement")
    @classmethod
    def accuracy_improvement_must_be_valid(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("accuracy_improvement must be between 0.0 and 1.0")
        return v

    @field_validator("confidence")
    @classmethod
    def confidence_must_be_valid(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v

    def format_report(self) -> str:
        """Return a human-readable Tree-of-Thought report."""
        lines = [
            f"=== Tree-of-Thought Report: {self.incident_id} ===",
            f"Hypotheses Generated: {len(self.hypotheses)}",
            f"Selected Hypothesis Index: {self.selected_hypothesis_index}",
            f"Accuracy Improvement: {self.accuracy_improvement:.2%}",
            f"Final Confidence: {self.confidence:.0%}",
            "",
            "Hypothesis Scores:",
        ]
        for h_id, score in self.hypothesis_scores.items():
            lines.append(f"  {h_id}: {score:.2%}")
        lines.append("")
        lines.append("Selected Hypothesis:")
        lines.append(f"  Scenario: {self.selected_hypothesis.scenario}")
        lines.append(f"  Root Cause: {self.selected_hypothesis.root_cause}")
        lines.append(f"  Confidence: {self.selected_hypothesis.confidence:.0%}")
        lines.append("")
        lines.append("Plan-and-Solve Validation:")
        lines.append(f"  {self.plan_and_solve_validation}")
        lines.append("")
        lines.append(f"Recommendation: {self.recommendation}")
        lines.append("")
        lines.append(f"Final Diagnosis (confidence {self.final_diagnosis.confidence:.0%}):")
        lines.append(f"  Root Cause: {self.final_diagnosis.root_cause}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Return JSON serialization of the Tree-of-Thought result."""
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str) -> TreeOfThoughtResult:
        """Parse a TreeOfThoughtResult from a JSON string."""
        return cls.model_validate_json(data)

    def model_dump(self, *args, **kwargs) -> dict:
        """Return a dict representation, converting Enums to values."""
        data = super().model_dump(*args, **kwargs)
        if isinstance(data.get("risk_tier"), RiskTier):
            data["risk_tier"] = self.risk_tier.value
        if isinstance(data.get("final_diagnosis"), Diagnosis):
            data["final_diagnosis"] = self.final_diagnosis.model_dump()
        return data
