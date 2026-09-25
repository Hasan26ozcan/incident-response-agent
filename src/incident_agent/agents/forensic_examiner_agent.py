"""Forensic examiner agent for the debate mechanism (Stage 8).

The ForensicExaminerAgent challenges the initial diagnosis by
looking for false positives, alternative explanations, and
weak evidence. It produces challenges that the debate mechanism
evaluates against the root-cause arguments.

Stage 8: Debate mechanism — root-cause agent vs. forensic examiner.
"""

from __future__ import annotations

from incident_agent.schemas.debate import Argument
from incident_agent.schemas.diagnosis import Diagnosis


class ForensicExaminerAgent:
    """Agent that challenges the initial diagnosis looking for false positives.

    The ForensicExaminerAgent critically examines the diagnosis
    for potential false positives, alternative explanations,
    weak evidence, and misclassifications. It produces challenge
    arguments that may lead to a revised diagnosis.

    All outputs are validated ``Argument`` Pydantic objects.
    """

    def __init__(self, confidence_threshold: float = 0.5) -> None:
        self.confidence_threshold = confidence_threshold
        self.challenges: list[Argument] = []

    def build_challenges(self, diagnosis: Diagnosis) -> list[Argument]:
        """Build challenges to the initial diagnosis.

        Examines the diagnosis for potential false positives,
        alternative explanations, and weak evidence points.

        Args:
            diagnosis: The initial diagnosis to challenge.

        Returns:
            A list of Argument objects challenging the root cause.
        """
        self.challenges = []

        # Challenge 1: Evidence quality concerns
        weak_evidence = [e for e in diagnosis.evidence if e.confidence_weight < 0.5]
        if weak_evidence:
            self.challenges.append(
                Argument(
                    agent="forensic_examiner",
                    point=(
                        f"{len(weak_evidence)} evidence item(s) have low confidence "
                        f"weights (< 0.5), potentially weakening the diagnosis"
                    ),
                    evidence_refs=[e.source for e in weak_evidence[:3]],
                    confidence=min(0.5 + len(weak_evidence) * 0.1, 0.85),
                )
            )

        # Challenge 2: Alternative explanations
        root_cause_lower = diagnosis.root_cause.lower()
        if "cpu" in root_cause_lower or "memory" in root_cause_lower:
            self.challenges.append(
                Argument(
                    agent="forensic_examiner",
                    point=(
                        "The diagnosis may conflate correlation with causation — "
                        "resource exhaustion symptoms can have multiple root causes, "
                        "including configuration issues or traffic spikes"
                    ),
                    evidence_refs=[],
                    confidence=0.6,
                )
            )

        # Challenge 3: Confidence boundary check
        if diagnosis.confidence < self.confidence_threshold:
            self.challenges.append(
                Argument(
                    agent="forensic_examiner",
                    point=(
                        f"Diagnosis confidence of {diagnosis.confidence:.0%} is "
                        f"below the {self.confidence_threshold:.0%} threshold — "
                        f"the diagnosis may be a false positive"
                    ),
                    evidence_refs=[],
                    confidence=0.75,
                )
            )

        # Challenge 4: Limited evidence diversity
        evidence_types = {e.source_type.value for e in diagnosis.evidence}
        if len(evidence_types) < 2:
            self.challenges.append(
                Argument(
                    agent="forensic_examiner",
                    point=(
                        f"Diagnosis relies on evidence from only {len(evidence_types)} "
                        f"source type(s). A more diverse evidence base is needed "
                        f"to rule out alternative explanations"
                    ),
                    evidence_refs=[],
                    confidence=0.65,
                )
            )

        # Challenge 5: Evidence count concern
        if len(diagnosis.evidence) < 3:
            self.challenges.append(
                Argument(
                    agent="forensic_examiner",
                    point=(
                        f"Only {len(diagnosis.evidence)} evidence item(s) support "
                        f"this diagnosis — insufficient for a high-confidence conclusion"
                    ),
                    evidence_refs=[],
                    confidence=0.7,
                )
            )

        return self.challenges

    def challenge(self, diagnosis: Diagnosis) -> list[Argument]:
        """Challenge the diagnosis by looking for false positives.

        Args:
            diagnosis: The initial diagnosis to challenge.

        Returns:
            A list of Argument objects challenging the diagnosis.
        """
        return self.build_challenges(diagnosis)
