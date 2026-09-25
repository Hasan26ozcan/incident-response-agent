"""Debate mechanism orchestrating the root-cause vs forensic examiner debate (Stage 8).

The DebateMechanism orchestrates a structured debate between
a RootCauseAgent (arguing for the initial diagnosis) and a
ForensicExaminerAgent (challenging it). The debate produces a
DebateOutcome containing the before/after false-positive rate
comparison and a final verdict.

Stage 8: Debate mechanism — root-cause agent vs. forensic examiner.
"""

from __future__ import annotations

from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
from incident_agent.schemas.agent_output import ReasoningStep
from incident_agent.schemas.debate import Argument, DebateOutcome
from incident_agent.schemas.diagnosis import Diagnosis


class DebateMechanism:
    """Orchestrates the debate between root-cause and forensic examiner agents.

    The debate mechanism:
    1. Receives an initial Diagnosis
    2. RootCauseAgent produces supporting arguments
    3. ForensicExaminerAgent produces challenges
    4. Both sides exchange arguments for multiple rounds
    5. False-positive rates are calculated before and after
    6. A verdict is determined
    7. A final Diagnosis is produced (confirmed, challenged, or revised)

    The false-positive rate comparison is the key output — showing
    whether the debate improved or worsened the diagnostic accuracy.
    """

    def __init__(
        self,
        debate_rounds: int = 2,
        confidence_threshold: float = 0.7,
    ) -> None:
        self.debate_rounds = debate_rounds
        self.confidence_threshold = confidence_threshold
        self.root_cause_agent = RootCauseAgent()
        self.forensic_examiner = ForensicExaminerAgent()

    def calculate_false_positive_rate(
        self,
        confidence: float,
        evidence_count: int,
        challenge_count: int,
    ) -> float:
        """Calculate a deterministic false-positive rate.

        The false-positive rate is inversely related to confidence
        and evidence count, and positively related to challenges.
        Lower confidence + fewer evidence = higher FPR.
        More challenges = higher FPR.

        Args:
            confidence: The diagnosis confidence (0.0-1.0).
            evidence_count: Number of evidence items.
            challenge_count: Number of forensic challenges.

        Returns:
            A false-positive rate between 0.0 and 1.0.
        """
        # Base FPR: inversely proportional to confidence
        base_fpr = 1.0 - confidence

        # Evidence reduces FPR (more evidence = more confident)
        evidence_reduction = min(evidence_count * 0.02, 0.2)

        # Challenges increase FPR (more challenges = more uncertainty)
        challenge_increase = min(challenge_count * 0.03, 0.3)

        fpr = max(0.0, min(1.0, base_fpr + challenge_increase - evidence_reduction))
        return round(fpr, 4)

    def determine_verdict(
        self,
        root_cause_strength: float,
        challenge_strength: float,
    ) -> str:
        """Determine the debate verdict.

        Args:
            root_cause_strength: Average confidence of root-cause arguments.
            challenge_strength: Average confidence of forensic challenges.

        Returns:
            Verdict: "confirmed", "challenged", or "revised".
        """
        margin = root_cause_strength - challenge_strength

        if margin > 0.2:
            # Root cause significantly stronger
            return "confirmed"
        if margin < -0.1:
            # Challenges stronger than root cause
            return "revised"
        # Close or moderate margin
        return "challenged"

    def run(self, diagnosis: Diagnosis) -> DebateOutcome:
        """Execute the full debate pipeline.

        Pipeline:
        1. RootCauseAgent builds supporting arguments
        2. ForensicExaminerAgent builds challenges
        3. Exchange arguments for debate_rounds
        4. Calculate FPR before debate
        5. Determine verdict
        6. Adjust confidence based on debate outcome
        7. Calculate FPR after debate
        8. Produce DebateOutcome

        Args:
            diagnosis: The initial diagnosis to debate.

        Returns:
            A validated ``DebateOutcome`` object.
        """
        # Step 1: Root-cause agent builds arguments
        root_cause_args = self.root_cause_agent.build_arguments(diagnosis)

        # Step 2: Forensic examiner builds challenges
        forensic_challenges = self.forensic_examiner.build_challenges(diagnosis)

        # Step 3: Debate rounds — agents respond to each other
        for round_num in range(self.debate_rounds):
            # Root cause responds to forensic challenges
            for challenge in forensic_challenges:
                if challenge.confidence > 0.6:
                    root_cause_args.append(
                        Argument(
                            agent="root_cause",
                            point=(f"Rebuttal to challenge: {challenge.point[:60]}..."),
                            evidence_refs=challenge.evidence_refs,
                            confidence=min(challenge.confidence + 0.05, 0.95),
                        )
                    )

            # Forensic examiner responds to root-cause arguments
            for arg in root_cause_args:
                if arg.confidence > 0.7:
                    forensic_challenges.append(
                        Argument(
                            agent="forensic_examiner",
                            point=(f"Counter-argument to {arg.point[:60]}..."),
                            evidence_refs=arg.evidence_refs,
                            confidence=min(arg.confidence * 0.9, 0.85),
                        )
                    )

        # Step 4: Calculate FPR before debate
        fpr_before = self.calculate_false_positive_rate(
            confidence=diagnosis.confidence,
            evidence_count=len(diagnosis.evidence),
            challenge_count=0,
        )

        # Step 5: Calculate argument strengths
        root_cause_strength = (
            sum(arg.confidence for arg in root_cause_args) / len(root_cause_args) if root_cause_args else 0.5
        )
        challenge_strength = (
            sum(chal.confidence for chal in forensic_challenges) / len(forensic_challenges)
            if forensic_challenges
            else 0.5
        )

        # Step 6: Determine verdict
        verdict = self.determine_verdict(root_cause_strength, challenge_strength)

        # Step 7: Adjust confidence based on verdict
        if verdict == "confirmed":
            confidence_adjustment = 0.05
            final_confidence = min(diagnosis.confidence + confidence_adjustment, 0.95)
        elif verdict == "challenged":
            confidence_adjustment = -0.05
            final_confidence = max(diagnosis.confidence + confidence_adjustment, 0.3)
        else:  # revised
            confidence_adjustment = -0.1
            final_confidence = max(diagnosis.confidence + confidence_adjustment, 0.2)

        # Step 8: Calculate FPR after debate
        fpr_after = self.calculate_false_positive_rate(
            confidence=final_confidence,
            evidence_count=len(diagnosis.evidence),
            challenge_count=len(forensic_challenges),
        )

        # Step 9: Build the final diagnosis
        final_diagnosis = Diagnosis(
            agent_type="debate_outcome",
            incident_id=diagnosis.incident_id,
            root_cause=diagnosis.root_cause,
            confidence=final_confidence,
            evidence=diagnosis.evidence,
            affected_service=diagnosis.affected_service,
            category=diagnosis.category,
            reasoning_steps=diagnosis.reasoning_steps
            + [
                ReasoningStep(
                    step_number=len(diagnosis.reasoning_steps) + 1,
                    description=f"Debate verdict: {verdict} (confidence adjusted by {confidence_adjustment:+.2f})",
                    evidence_refs=[],
                ),
            ],
            recommendation=diagnosis.recommendation,
            risk_tier=diagnosis.risk_tier,
        )

        # Step 10: Build reasoning steps
        reasoning_steps = [
            ReasoningStep(
                step_number=1,
                description=f"Root-cause agent produced {len(root_cause_args)} supporting arguments",
                evidence_refs=[],
            ),
            ReasoningStep(
                step_number=2,
                description=f"Forensic examiner raised {len(forensic_challenges)} challenges",
                evidence_refs=[],
            ),
            ReasoningStep(
                step_number=3,
                description=f"Debate concluded with verdict: {verdict}",
                evidence_refs=[],
            ),
            ReasoningStep(
                step_number=4,
                description=(
                    f"False-positive rate changed from {fpr_before:.2%} to {fpr_after:.2%} "
                    f"(improvement: {(fpr_before - fpr_after):.2%})"
                ),
                evidence_refs=[],
            ),
        ]

        # Step 11: Build recommendation
        if verdict == "confirmed":
            recommendation = (
                f"Debate confirmed the original diagnosis of {diagnosis.root_cause}. "
                f"The initial assessment was accurate. Proceed with the recommended actions."
            )
        elif verdict == "challenged":
            recommendation = (
                "Debate challenged the diagnosis. Further investigation is recommended "
                "before taking action. Consider additional evidence sources."
            )
        else:
            recommendation = (
                "Debate led to a revision of the diagnosis. The original root cause "
                "may be incorrect. Re-examine the evidence and consider alternative explanations."
            )

        # Step 12: Create the DebateOutcome
        debate_outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id=diagnosis.incident_id,
            original_diagnosis=diagnosis,
            root_cause_arguments=root_cause_args,
            forensic_challenges=forensic_challenges,
            verdict=verdict,
            confidence=final_confidence,
            false_positive_rate_before=fpr_before,
            false_positive_rate_after=fpr_after,
            confidence_adjustment=confidence_adjustment,
            final_diagnosis=final_diagnosis,
            debate_rounds=self.debate_rounds,
            reasoning_steps=reasoning_steps,
            recommendation=recommendation,
        )

        return debate_outcome
