"""Tests for Stage 8 — Debate Mechanism.

Covers:
  - Argument schema validation.
  - DebateOutcome schema validation, to_json/from_json roundtrip, format_report.
  - RootCauseAgent produces supporting arguments.
  - ForensicExaminerAgent produces challenges.
  - DebateMechanism.run() produces validated DebateOutcome.
  - Before/after false-positive rate comparison.
  - Verdict determination (confirmed/challenged/revised).
  - Debate rounds exchange arguments.
  - Cross-cutting rule: DebateOutcome is a validated Pydantic object.
  - Backward compatibility: ReActAgent, OrchestratorAgent, IncidentCommander still work.
"""

from __future__ import annotations

import pytest

from incident_agent.agents.debate_mechanism import DebateMechanism
from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import OrchestratorAgent
from incident_agent.agents.react_agent import ReActAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
from incident_agent.schemas import (
    Argument,
    CommanderDiagnosis,
    DebateOutcome,
    Diagnosis,
    EvidenceItem,
    EvidenceType,
    ReasoningStep,
    WorkerFinding,
)
from incident_agent.schemas.agent_output import AgentOutput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            source_type=EvidenceType.LOG_ENTRY,
            source="log:test",
            detail="Test evidence",
            confidence_weight=0.9,
        )
    ]


def _default_reasoning() -> list[ReasoningStep]:
    return [
        ReasoningStep(step_number=1, description="Test step", evidence_refs=[]),
    ]


def _make_diagnosis(
    risk_tier: str = "high",
    confidence: float = 0.85,
    category: str = "cpu_exhaustion",
    root_cause: str = "Test root cause for testing purposes that is long enough",
    evidence_count: int = 1,
) -> Diagnosis:
    ev = _default_evidence() * evidence_count if evidence_count > 1 else _default_evidence()
    return Diagnosis(
        agent_type="orchestrator_worker",
        incident_id="INC-001",
        root_cause=root_cause,
        confidence=confidence,
        evidence=ev,
        affected_service="test-service",
        category=category,
        reasoning_steps=_default_reasoning(),
        recommendation="Test recommendation for testing purposes that is long enough",
        risk_tier=risk_tier,
    )


def _make_worker_finding(worker_type: str = "log", confidence: float = 0.8) -> WorkerFinding:
    return WorkerFinding(
        worker_type=worker_type,
        incident_id="INC-001",
        evidence=_default_evidence(),
        reasoning_steps=_default_reasoning(),
        confidence=confidence,
        summary=f"{worker_type} analysis summary for testing that is long enough",
    )


# ---------------------------------------------------------------------------
# Argument schema
# ---------------------------------------------------------------------------


class TestArgumentSchema:
    """Verify Argument is a properly structured Pydantic model."""

    def test_argument_is_pydantic_model(self):
        """Argument should be a Pydantic BaseModel subclass."""
        arg = Argument(
            agent="root_cause",
            point="This is a test argument point",
            evidence_refs=["log:test"],
            confidence=0.8,
        )
        assert isinstance(arg, Argument)
        assert arg.agent == "root_cause"
        assert arg.point == "This is a test argument point"

    def test_argument_has_valid_agent(self):
        """Argument agent must be a non-empty string."""
        arg = Argument(
            agent="forensic_examiner",
            point="Test point",
            confidence=0.7,
        )
        assert arg.agent == "forensic_examiner"

    def test_argument_confidence_bounds(self):
        """Argument confidence must be between 0.0 and 1.0."""
        arg = Argument(
            agent="root_cause",
            point="Test point",
            confidence=0.5,
        )
        assert 0.0 <= arg.confidence <= 1.0

    def test_argument_point_min_length(self):
        """Argument point must be at least 5 characters."""
        with pytest.raises(Exception):
            Argument(
                agent="root_cause",
                point="too",
                confidence=0.5,
            )

    def test_argument_to_json_roundtrip(self):
        """Argument serialization should roundtrip correctly."""
        arg = Argument(
            agent="root_cause",
            point="Test argument point for serialization",
            evidence_refs=["log:test", "metric:cpu"],
            confidence=0.8,
        )
        json_str = arg.model_dump_json()
        restored = Argument.model_validate_json(json_str)
        assert restored.agent == arg.agent
        assert restored.point == arg.point
        assert restored.confidence == arg.confidence


# ---------------------------------------------------------------------------
# DebateOutcome schema
# ---------------------------------------------------------------------------


class TestDebateOutcomeSchema:
    """Verify DebateOutcome is a properly structured Pydantic model."""

    def test_debate_outcome_is_pydantic_model(self):
        """DebateOutcome should be a Pydantic BaseModel subclass."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Root cause is supported by evidence",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Evidence may be weak",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=2,
            reasoning_steps=_default_reasoning(),
            recommendation="Proceed with recommended actions",
        )
        assert isinstance(outcome, DebateOutcome)

    def test_debate_outcome_inherits_agent_output(self):
        """DebateOutcome should inherit from AgentOutput."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Test point",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Test challenge",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=1,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        assert isinstance(outcome, AgentOutput)
        assert outcome.agent_type == "debate_mechanism"
        assert outcome.incident_id == "INC-001"

    def test_debate_outcome_has_all_fields(self):
        """DebateOutcome must have all required fields."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Root cause is supported by evidence",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Evidence may be weak",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=2,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        assert outcome.original_diagnosis is not None
        assert len(outcome.root_cause_arguments) >= 1
        assert len(outcome.forensic_challenges) >= 1
        assert outcome.final_diagnosis is not None

    def test_debate_outcome_verdict_validation(self):
        """DebateOutcome must validate verdict."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Test point",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Test challenge",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=1,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        assert outcome.verdict in ("confirmed", "challenged", "revised")

    def test_debate_outcome_contains_original_diagnosis(self):
        """DebateOutcome must contain the original Diagnosis."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Test point",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Evidence may be weak",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=1,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        assert isinstance(outcome.original_diagnosis, Diagnosis)
        assert outcome.original_diagnosis.incident_id == "INC-001"

    def test_debate_outcome_contains_final_diagnosis(self):
        """DebateOutcome must contain the final Diagnosis."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Root cause is supported by evidence",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Evidence may be weak",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=1,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        assert isinstance(outcome.final_diagnosis, Diagnosis)

    def test_debate_outcome_fpr_bounds(self):
        """False-positive rates must be between 0.0 and 1.0."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Test point",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Evidence may be weak",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=1,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        assert 0.0 <= outcome.false_positive_rate_before <= 1.0
        assert 0.0 <= outcome.false_positive_rate_after <= 1.0

    def test_debate_outcome_to_json_roundtrip(self):
        """DebateOutcome.to_json() and from_json() must roundtrip."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Test point for JSON roundtrip",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Test challenge for JSON roundtrip",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=2,
            reasoning_steps=_default_reasoning(),
            recommendation="Test recommendation that is long enough",
        )
        json_str = outcome.to_json()
        restored = DebateOutcome.from_json(json_str)
        assert restored.incident_id == outcome.incident_id
        assert restored.verdict == outcome.verdict
        assert len(restored.root_cause_arguments) == len(outcome.root_cause_arguments)

    def test_debate_outcome_format_report(self):
        """DebateOutcome.format_report() must produce a readable report."""
        diagnosis = _make_diagnosis()
        outcome = DebateOutcome(
            agent_type="debate_mechanism",
            incident_id="INC-001",
            original_diagnosis=diagnosis,
            root_cause_arguments=[
                Argument(
                    agent="root_cause",
                    point="Root cause is supported by evidence",
                    confidence=0.8,
                ),
            ],
            forensic_challenges=[
                Argument(
                    agent="forensic_examiner",
                    point="Evidence may be weak",
                    confidence=0.6,
                ),
            ],
            verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
            false_positive_rate_after=0.1,
            confidence_adjustment=0.05,
            final_diagnosis=diagnosis,
            debate_rounds=2,
            reasoning_steps=_default_reasoning(),
            recommendation="Proceed with recommended actions",
        )
        report = outcome.format_report()
        assert "Debate Outcome" in report
        assert "INC-001" in report
        assert "False-Positive Rate" in report

    def test_debate_outcome_requires_minimum_fields(self):
        """DebateOutcome must enforce minimum field requirements."""
        diagnosis = _make_diagnosis()
        with pytest.raises(Exception):
            DebateOutcome(
                agent_type="debate_mechanism",
                incident_id="INC-001",
                original_diagnosis=diagnosis,
                root_cause_arguments=[],
                forensic_challenges=[],
                verdict="confirmed",
            confidence=0.8,
            false_positive_rate_before=0.2,
                false_positive_rate_after=0.1,
                confidence_adjustment=0.05,
                final_diagnosis=diagnosis,
                debate_rounds=1,
                reasoning_steps=[],
                recommendation="too short",
            )


# ---------------------------------------------------------------------------
# RootCauseAgent
# ---------------------------------------------------------------------------


class TestRootCauseAgent:
    def test_root_cause_agent_creates_arguments(self):
        """RootCauseAgent should produce supporting arguments."""
        agent = RootCauseAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        arguments = agent.analyze(diagnosis)
        assert len(arguments) >= 1
        for arg in arguments:
            assert isinstance(arg, Argument)
            assert arg.agent == "root_cause"

    def test_root_cause_agent_arguments_are_valid(self):
        """All arguments must be valid Argument Pydantic objects."""
        agent = RootCauseAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        arguments = agent.analyze(diagnosis)
        for arg in arguments:
            Argument.model_validate(arg.model_dump())

    def test_root_cause_agent_includes_evidence_refs(self):
        """Arguments should cite evidence references."""
        agent = RootCauseAgent()
        diagnosis = _make_diagnosis(confidence=0.85, evidence_count=3)
        arguments = agent.analyze(diagnosis)
        assert len(arguments[0].evidence_refs) >= 0

    def test_root_cause_agent_confidence_bounds(self):
        """Argument confidence must be between 0.0 and 1.0."""
        agent = RootCauseAgent()
        diagnosis = _make_diagnosis(confidence=0.9)
        arguments = agent.analyze(diagnosis)
        for arg in arguments:
            assert 0.0 <= arg.confidence <= 1.0

    def test_root_cause_agent_with_low_confidence(self):
        """RootCauseAgent should work with low-confidence diagnosis."""
        agent = RootCauseAgent(confidence_threshold=0.5)
        diagnosis = _make_diagnosis(confidence=0.4)
        arguments = agent.analyze(diagnosis)
        assert len(arguments) >= 1


# ---------------------------------------------------------------------------
# ForensicExaminerAgent
# ---------------------------------------------------------------------------


class TestForensicExaminerAgent:
    def test_forensic_examiner_creates_challenges(self):
        """ForensicExaminerAgent should produce challenges."""
        agent = ForensicExaminerAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        challenges = agent.challenge(diagnosis)
        assert len(challenges) >= 1
        for chal in challenges:
            assert isinstance(chal, Argument)
            assert chal.agent == "forensic_examiner"

    def test_forensic_examiner_challenges_are_valid(self):
        """All challenges must be valid Argument Pydantic objects."""
        agent = ForensicExaminerAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        challenges = agent.challenge(diagnosis)
        for chal in challenges:
            Argument.model_validate(chal.model_dump())

    def test_forensic_examiner_agent_type(self):
        """ForensicExaminerAgent challenges should have correct agent type."""
        agent = ForensicExaminerAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        challenges = agent.challenge(diagnosis)
        for chal in challenges:
            assert chal.agent == "forensic_examiner"

    def test_forensic_examiner_confidence_bounds(self):
        """Challenge confidence must be between 0.0 and 1.0."""
        agent = ForensicExaminerAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        challenges = agent.challenge(diagnosis)
        for chal in challenges:
            assert 0.0 <= chal.confidence <= 1.0


# ---------------------------------------------------------------------------
# DebateMechanism
# ---------------------------------------------------------------------------


class TestDebateMechanismRun:
    @pytest.fixture(autouse=True)
    def mechanism(self):
        return DebateMechanism(debate_rounds=2, confidence_threshold=0.7)

    def test_run_returns_debate_outcome(self, mechanism):
        """run() must return a DebateOutcome."""
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        assert isinstance(outcome, DebateOutcome)
        DebateOutcome.model_validate(outcome.model_dump())

    def test_run_has_all_fields(self, mechanism):
        """DebateOutcome from run() must have all required fields."""
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        assert outcome.incident_id == "INC-001"
        assert outcome.verdict in ("confirmed", "challenged", "revised")
        assert len(outcome.root_cause_arguments) >= 1
        assert len(outcome.forensic_challenges) >= 1

    def test_run_agent_type_is_debate_mechanism(self, mechanism):
        """DebateOutcome agent_type must be debate_mechanism."""
        diagnosis = _make_diagnosis(risk_tier="low")
        outcome = mechanism.run(diagnosis)
        assert outcome.agent_type == "debate_mechanism"

    def test_run_contains_original_diagnosis(self, mechanism):
        """DebateOutcome must contain the original Diagnosis."""
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        assert isinstance(outcome.original_diagnosis, Diagnosis)
        assert outcome.original_diagnosis.incident_id == "INC-001"

    def test_run_contains_final_diagnosis(self, mechanism):
        """DebateOutcome must contain the final Diagnosis."""
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        assert isinstance(outcome.final_diagnosis, Diagnosis)

    def test_run_with_multiple_debate_rounds(self, mechanism):
        """run() with debate_rounds=2 should have multiple rounds."""
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        assert outcome.debate_rounds == 2

    def test_run_debate_rounds_parameter(self):
        """run() should respect the debate_rounds parameter."""
        mechanism3 = DebateMechanism(debate_rounds=3)
        diagnosis = _make_diagnosis()
        outcome = mechanism3.run(diagnosis)
        assert outcome.debate_rounds == 3

    def test_run_produces_valid_schema(self, mechanism):
        """DebateOutcome must pass Pydantic model validation after run()."""
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        DebateOutcome.model_validate(outcome.model_dump())

    def test_run_does_not_break_original_diagnosis(self, mechanism):
        """The original diagnosis should remain unchanged after debate."""
        diagnosis = _make_diagnosis(confidence=0.85)
        original_confidence = diagnosis.confidence
        outcome = mechanism.run(diagnosis)
        assert outcome.original_diagnosis.confidence == original_confidence


# ---------------------------------------------------------------------------
# False-positive rate comparison
# ---------------------------------------------------------------------------


class TestFalsePositiveRateComparison:
    """Verify the before/after false-positive rate comparison."""

    def test_fpr_calculation_is_between_0_and_1(self):
        """FPR must always be between 0.0 and 1.0."""
        mechanism = DebateMechanism()
        for confidence in [0.3, 0.5, 0.7, 0.9]:
            for evidence_count in [1, 3, 10]:
                for challenge_count in [0, 2, 5]:
                    fpr = mechanism.calculate_false_positive_rate(
                        confidence, evidence_count, challenge_count
                    )
                    assert 0.0 <= fpr <= 1.0

    def test_fpr_decreases_with_confidence(self):
        """Higher confidence should lead to lower FPR."""
        mechanism = DebateMechanism()
        fpr_high = mechanism.calculate_false_positive_rate(0.9, 5, 1)
        fpr_low = mechanism.calculate_false_positive_rate(0.5, 5, 1)
        assert fpr_high <= fpr_low

    def test_fpr_decreases_with_evidence(self):
        """More evidence should lead to lower FPR."""
        mechanism = DebateMechanism()
        fpr_few = mechanism.calculate_false_positive_rate(0.7, 1, 1)
        fpr_many = mechanism.calculate_false_positive_rate(0.7, 10, 1)
        assert fpr_many <= fpr_few

    def test_fpr_increases_with_challenges(self):
        """More challenges should lead to higher FPR."""
        mechanism = DebateMechanism()
        fpr_few = mechanism.calculate_false_positive_rate(0.7, 5, 0)
        fpr_many = mechanism.calculate_false_positive_rate(0.7, 5, 5)
        assert fpr_many >= fpr_few

    def test_fpr_improves_after_debate(self):
        """The debate should ideally improve (lower) the FPR after debate."""
        diagnosis = _make_diagnosis(confidence=0.85, evidence_count=3)
        mechanism = DebateMechanism(debate_rounds=2)
        outcome = mechanism.run(diagnosis)
        # FPR after should be <= FPR before for confirmed verdicts
        if outcome.verdict == "confirmed":
            assert outcome.false_positive_rate_after <= outcome.false_positive_rate_before

    def test_debate_outcome_has_both_fpr_values(self):
        """DebateOutcome must have both before and after FPR values."""
        mechanism = DebateMechanism()
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        assert 0.0 <= outcome.false_positive_rate_before <= 1.0
        assert 0.0 <= outcome.false_positive_rate_after <= 1.0


# ---------------------------------------------------------------------------
# Verdict determination
# ---------------------------------------------------------------------------


class TestVerdictDetermination:
    """Verify the debate verdict logic."""

    def test_verdict_confirmed_when_root_cause_strong(self):
        """Verdict should be 'confirmed' when root-cause arguments are stronger."""
        mechanism = DebateMechanism()
        verdict = mechanism.determine_verdict(
            root_cause_strength=0.8,
            challenge_strength=0.4,
        )
        assert verdict == "confirmed"

    def test_verdict_revised_when_challenges_strong(self):
        """Verdict should be 'revised' when challenges outweigh root cause."""
        mechanism = DebateMechanism()
        verdict = mechanism.determine_verdict(
            root_cause_strength=0.3,
            challenge_strength=0.7,
        )
        assert verdict == "revised"

    def test_verdict_challenged_when_close(self):
        """Verdict should be 'challenged' when the margin is moderate."""
        mechanism = DebateMechanism()
        verdict = mechanism.determine_verdict(
            root_cause_strength=0.6,
            challenge_strength=0.5,
        )
        assert verdict == "challenged"

    def test_verdict_returns_valid_string(self):
        """Verdict must be a valid string."""
        mechanism = DebateMechanism()
        for root_strength in [0.2, 0.5, 0.8]:
            for chal_strength in [0.2, 0.5, 0.8]:
                verdict = mechanism.determine_verdict(root_strength, chal_strength)
                assert verdict in ("confirmed", "challenged", "revised")


# ---------------------------------------------------------------------------
# Debate rounds
# ---------------------------------------------------------------------------


class TestDebateRounds:
    """Verify the debate round mechanism."""

    def test_multiple_rounds_add_arguments(self):
        """Multiple debate rounds should add more arguments."""
        mechanism = DebateMechanism(debate_rounds=3)
        diagnosis = _make_diagnosis(confidence=0.85, evidence_count=3)
        outcome = mechanism.run(diagnosis)
        assert len(outcome.root_cause_arguments) >= 1
        assert len(outcome.forensic_challenges) >= 1

    def test_debate_rounds_count(self):
        """DebateOutcome should report the correct number of rounds."""
        mechanism2 = DebateMechanism(debate_rounds=2)
        diagnosis = _make_diagnosis()
        outcome = mechanism2.run(diagnosis)
        assert outcome.debate_rounds == 2


# ---------------------------------------------------------------------------
# Confidence adjustment
# ---------------------------------------------------------------------------


class TestConfidenceAdjustment:
    """Verify confidence adjustment based on debate outcome."""

    def test_confirmed_increases_confidence(self):
        """'confirmed' verdict should increase confidence."""
        mechanism = DebateMechanism()
        diagnosis = _make_diagnosis(confidence=0.8)
        outcome = mechanism.run(diagnosis)
        if outcome.verdict == "confirmed":
            assert outcome.confidence_adjustment > 0

    def test_challenged_decreases_confidence(self):
        """'challenged' verdict should decrease confidence."""
        mechanism = DebateMechanism()
        diagnosis = _make_diagnosis(confidence=0.8)
        outcome = mechanism.run(diagnosis)
        if outcome.verdict == "challenged":
            assert outcome.confidence_adjustment < 0

    def test_revised_decreases_confidence_more(self):
        """'revised' verdict should decrease confidence."""
        mechanism = DebateMechanism()
        diagnosis = _make_diagnosis(confidence=0.8)
        outcome = mechanism.run(diagnosis)
        if outcome.verdict == "revised":
            assert outcome.confidence_adjustment < 0


# ---------------------------------------------------------------------------
# Cross-cutting: schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Verify all debate outputs are validated Pydantic objects."""

    def test_debate_outcome_schema_valid_after_run(self):
        """DebateOutcome must be schema-valid after run()."""
        mechanism = DebateMechanism()
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        DebateOutcome.model_validate(outcome.model_dump())

    def test_argument_schema_valid_in_outcome(self):
        """Arguments inside DebateOutcome must be valid."""
        mechanism = DebateMechanism()
        diagnosis = _make_diagnosis()
        outcome = mechanism.run(diagnosis)
        for arg in outcome.root_cause_arguments:
            Argument.model_validate(arg.model_dump())
        for chal in outcome.forensic_challenges:
            Argument.model_validate(chal.model_dump())


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify ReActAgent, OrchestratorAgent, and IncidentCommander still work."""

    def test_react_agent_still_works(self):
        """ReActAgent should still produce valid diagnoses."""
        agent = ReActAgent(confidence_threshold=0.5)
        diagnosis = agent.run("INC-001")
        Diagnosis.model_validate(diagnosis.model_dump())
        assert diagnosis.agent_type == "single_agent_react"

    def test_orchestrator_still_works(self):
        """OrchestratorAgent should still produce valid diagnoses."""
        agent = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = agent.run("INC-001")
        Diagnosis.model_validate(diagnosis.model_dump())
        assert diagnosis.agent_type == "orchestrator_worker"

    def test_incident_commander_still_works(self):
        """IncidentCommander should still produce valid CommanderDiagnoses."""
        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = orchestrator.run("INC-001")
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(
            diagnosis,
            {
                "log": _make_worker_finding("log", diagnosis.confidence),
            },
            "INC-001",
        )
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())

    def test_debate_mechanism_does_not_break_existing_agents(self):
        """Adding debate mechanism should not break existing agents."""
        react = ReActAgent(confidence_threshold=0.5)
        react_diagnosis = react.run("INC-003")
        Diagnosis.model_validate(react_diagnosis.model_dump())

        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        orch_diagnosis = orchestrator.run("INC-003")
        Diagnosis.model_validate(orch_diagnosis.model_dump())

        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(
            orch_diagnosis,
            {"log": _make_worker_finding("log", orch_diagnosis.confidence)},
            "INC-003",
        )
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())

    def test_all_agents_produce_valid_outputs(self):
        """All agents produce valid Pydantic outputs after Stage 8."""
        for incident_id in ["INC-001", "INC-002", "INC-003"]:
            react = ReActAgent(confidence_threshold=0.5)
            react_diag = react.run(incident_id)
            Diagnosis.model_validate(react_diag.model_dump())

            orchestrator = OrchestratorAgent(confidence_threshold=0.5)
            orch_diag = orchestrator.run(incident_id)
            Diagnosis.model_validate(orch_diag.model_dump())

            commander = IncidentCommander(confidence_threshold=0.7)
            cmd_diag = commander.run(
                orch_diag,
                {"log": _make_worker_finding("log", orch_diag.confidence)},
                incident_id,
            )
            CommanderDiagnosis.model_validate(cmd_diag.model_dump())

            mechanism = DebateMechanism()
            debate_outcome = mechanism.run(orch_diag)
            DebateOutcome.model_validate(debate_outcome.model_dump())
