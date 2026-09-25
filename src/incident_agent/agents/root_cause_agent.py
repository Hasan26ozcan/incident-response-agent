"""Root-cause agent for the debate mechanism (Stage 8).

The RootCauseAgent argues in favor of the initial diagnosis's
root cause. It produces supporting arguments citing evidence
from the diagnosis, reinforcing the original findings.

Stage 8: Debate mechanism — root-cause agent vs. forensic examiner.
"""

from __future__ import annotations

from incident_agent.schemas.debate import Argument
from incident_agent.schemas.diagnosis import Diagnosis


class RootCauseAgent:
    """Agent that argues FOR the initial diagnosis's root cause.

    The RootCauseAgent examines the diagnosis and produces
    arguments supporting the root-cause conclusion. It cites
    evidence items and reasoning steps from the diagnosis,
    reinforcing the original findings with additional analysis.

    All outputs are validated ``Argument`` Pydantic objects.
    """

    def __init__(self, confidence_threshold: float = 0.6) -> None:
        self.confidence_threshold = confidence_threshold
        self.arguments: list[Argument] = []

    def build_arguments(self, diagnosis: Diagnosis) -> list[Argument]:
        """Build supporting arguments for the diagnosis's root cause.

        Examines the diagnosis evidence and reasoning steps to
        produce arguments that reinforce the root-cause conclusion.

        Args:
            diagnosis: The initial diagnosis to defend.

        Returns:
            A list of Argument objects supporting the root cause.
        """
        self.arguments = []

        # Argument 1: Evidence supports root cause
        evidence_points = len(diagnosis.evidence)
        self.arguments.append(
            Argument(
                agent="root_cause",
                point=(
                    f"Root cause '{diagnosis.root_cause}' is supported by "
                    f"{evidence_points} evidence items across multiple data sources"
                ),
                evidence_refs=[e.source for e in diagnosis.evidence[:3]],
                confidence=min(0.6 + evidence_points * 0.05, 0.95),
            )
        )

        # Argument 2: Reasoning chain is coherent
        reasoning_count = len(diagnosis.reasoning_steps)
        self.arguments.append(
            Argument(
                agent="root_cause",
                point=(
                    f"The reasoning chain contains {reasoning_count} coherent "
                    f"steps that logically support the root-cause conclusion"
                ),
                evidence_refs=[],
                confidence=min(0.65 + reasoning_count * 0.03, 0.95),
            )
        )

        # Argument 3: Confidence meets threshold
        if diagnosis.confidence >= self.confidence_threshold:
            self.arguments.append(
                Argument(
                    agent="root_cause",
                    point=(
                        f"Diagnosis confidence of {diagnosis.confidence:.0%} "
                        f"meets or exceeds the {self.confidence_threshold:.0%} threshold"
                    ),
                    evidence_refs=[],
                    confidence=diagnosis.confidence,
                )
            )

        # Argument 4: Risk tier justification
        risk_label = (
            diagnosis.risk_tier.value
            if isinstance(diagnosis.risk_tier, type(diagnosis.risk_tier))
            else str(diagnosis.risk_tier)
        )
        self.arguments.append(
            Argument(
                agent="root_cause",
                point=(
                    f"The {risk_label}-risk tier classification is justified by "
                    f"the evidence and affects {diagnosis.affected_service}"
                ),
                evidence_refs=[],
                confidence=min(0.7 + len(diagnosis.evidence) * 0.02, 0.95),
            )
        )

        return self.arguments

    def analyze(self, diagnosis: Diagnosis) -> list[Argument]:
        """Analyze the diagnosis and produce supporting arguments.

        Args:
            diagnosis: The initial diagnosis to defend.

        Returns:
            A list of Argument objects supporting the root cause.
        """
        return self.build_arguments(diagnosis)
