"""Tests for Stage 9 — Tree-of-Thought + Plan-and-Solve.

Covers:
  - Hypothesis schema validation.
  - TreeOfThoughtResult schema validation, to_json/from_json roundtrip, format_report.
  - TreeOfThoughtAgent generates multiple hypotheses.
  - Hypothesis scoring and ranking.
  - Best hypothesis selection.
  - Plan-and-Solve validation.
  - run() produces validated TreeOfThoughtResult.
  - Accuracy improvement is measurable.
  - Cross-cutting rule: TreeOfThoughtResult is a validated Pydantic object.
  - Backward compatibility: ReActAgent, OrchestratorAgent, IncidentCommander, DebateMechanism still work.
"""

from __future__ import annotations

import pytest

from incident_agent.agents.debate_mechanism import DebateMechanism
from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import OrchestratorAgent
from incident_agent.agents.react_agent import ReActAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
from incident_agent.agents.tree_of_thought_agent import TreeOfThoughtAgent
from incident_agent.schemas import (
    Argument,
    CommanderDiagnosis,
    DebateOutcome,
    Diagnosis,
    EvidenceItem,
    EvidenceType,
    Hypothesis,
    ReasoningStep,
    TreeOfThoughtResult,
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
    evidence_count: int = 3,
) -> Diagnosis:
    evidence = [
        EvidenceItem(
            source_type=EvidenceType.LOG_ENTRY,
            source=f"log:test-{i}",
            detail=f"Test evidence {i}",
            confidence_weight=0.9,
        )
        for i in range(evidence_count)
    ]
    return Diagnosis(
        agent_type="orchestrator_worker",
        incident_id="INC-001",
        root_cause=root_cause,
        confidence=confidence,
        evidence=evidence,
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
# Hypothesis schema
# ---------------------------------------------------------------------------


class TestHypothesisSchema:
    """Verify Hypothesis is a properly structured Pydantic model."""

    def test_hypothesis_is_pydantic_model(self):
        """Hypothesis should be a Pydantic BaseModel subclass."""
        hyp = Hypothesis(
            hypothesis_id="hot-INC-001-0",
            scenario="Test scenario for hypothesis",
            root_cause="Test root cause",
            confidence=0.8,
            category="cpu_exhaustion",
            risk_tier="high",
        )
        assert isinstance(hyp, Hypothesis)
        assert hyp.hypothesis_id == "hot-INC-001-0"
        assert hyp.scenario == "Test scenario for hypothesis"

    def test_hypothesis_has_valid_id(self):
        """Hypothesis ID must be a non-empty string."""
        hyp = Hypothesis(
            hypothesis_id="hot-INC-001-1",
            scenario="Test scenario for hypothesis",
            root_cause="Test root cause",
            confidence=0.7,
            category="cpu_exhaustion",
            risk_tier="high",
        )
        assert hyp.hypothesis_id == "hot-INC-001-1"

    def test_hypothesis_confidence_bounds(self):
        """Hypothesis confidence must be between 0.0 and 1.0."""
        hyp = Hypothesis(
            hypothesis_id="hot-INC-001-0",
            scenario="Test scenario for hypothesis",
            root_cause="Test root cause",
            confidence=0.5,
            category="cpu_exhaustion",
            risk_tier="high",
        )
        assert 0.0 <= hyp.confidence <= 1.0

    def test_hypothesis_scenario_min_length(self):
        """Hypothesis scenario must be at least 10 characters."""
        with pytest.raises(Exception):
            Hypothesis(
                hypothesis_id="hot-INC-001-0",
                scenario="too short",
                root_cause="Test root cause",
                confidence=0.5,
                category="cpu_exhaustion",
                risk_tier="high",
            )

    def test_hypothesis_root_cause_min_length(self):
        """Hypothesis root_cause must be at least 5 characters."""
        with pytest.raises(Exception):
            Hypothesis(
                hypothesis_id="hot-INC-001-0",
                scenario="Test scenario for hypothesis",
                root_cause="too",
                confidence=0.5,
                category="cpu_exhaustion",
                risk_tier="high",
            )

    def test_hypothesis_to_json_roundtrip(self):
        """Hypothesis serialization should roundtrip correctly."""
        hyp = Hypothesis(
            hypothesis_id="hot-INC-001-0",
            scenario="Test scenario for JSON serialization",
            root_cause="Test root cause",
            confidence=0.8,
            category="cpu_exhaustion",
            risk_tier="high",
        )
        json_str = hyp.model_dump_json()
        restored = Hypothesis.model_validate_json(json_str)
        assert restored.hypothesis_id == hyp.hypothesis_id
        assert restored.scenario == hyp.scenario
        assert restored.confidence == hyp.confidence

    def test_hypothesis_with_supporting_evidence(self):
        """Hypothesis can carry supporting evidence items."""
        evidence = _default_evidence()
        hyp = Hypothesis(
            hypothesis_id="hot-INC-001-0",
            scenario="Test scenario with evidence",
            root_cause="Test root cause",
            confidence=0.8,
            category="cpu_exhaustion",
            risk_tier="high",
            supporting_evidence=evidence,
        )
        assert len(hyp.supporting_evidence) == 1


# ---------------------------------------------------------------------------
# TreeOfThoughtResult schema
# ---------------------------------------------------------------------------


class TestTreeOfThoughtResultSchema:
    """Verify TreeOfThoughtResult is a properly structured Pydantic model."""

    def test_tot_result_is_pydantic_model(self):
        """TreeOfThoughtResult should be a Pydantic BaseModel subclass."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert isinstance(result, TreeOfThoughtResult)

    def test_tot_result_inherits_agent_output(self):
        """TreeOfThoughtResult should inherit from AgentOutput."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert isinstance(result, AgentOutput)
        assert result.agent_type == "tree_of_thought"
        assert result.incident_id == "INC-001"

    def test_tot_result_has_all_fields(self):
        """TreeOfThoughtResult must have all required fields."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert result.incident_id == "INC-001"
        assert len(result.hypotheses) >= 2
        assert result.selected_hypothesis is not None
        assert result.final_diagnosis is not None
        assert len(result.reasoning_steps) >= 1

    def test_tot_result_requires_minimum_hypotheses(self):
        """TreeOfThoughtResult must have at least 2 hypotheses."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent(num_hypotheses=2)
        result = agent.run(diagnosis)
        assert len(result.hypotheses) >= 2

    def test_tot_result_has_valid_selected_index(self):
        """Selected hypothesis index must be valid."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert 0 <= result.selected_hypothesis_index < len(result.hypotheses)

    def test_tot_result_has_hypothesis_scores(self):
        """TreeOfThoughtResult must have hypothesis scores mapping."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert len(result.hypothesis_scores) >= 2
        for h_id, score in result.hypothesis_scores.items():
            assert 0.0 <= score <= 1.0

    def test_tot_result_accuracy_improvement_bounds(self):
        """Accuracy improvement must be between 0.0 and 1.0."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert 0.0 <= result.accuracy_improvement <= 1.0

    def test_tot_result_confidence_bounds(self):
        """Final confidence must be between 0.0 and 1.0."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        assert 0.0 <= result.confidence <= 1.0

    def test_tot_result_to_json_roundtrip(self):
        """TreeOfThoughtResult.to_json() and from_json() must roundtrip."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        json_str = result.to_json()
        restored = TreeOfThoughtResult.from_json(json_str)
        assert restored.incident_id == result.incident_id
        assert len(restored.hypotheses) == len(result.hypotheses)
        assert restored.selected_hypothesis_index == result.selected_hypothesis_index

    def test_tot_result_format_report(self):
        """TreeOfThoughtResult.format_report() must produce a readable report."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        report = result.format_report()
        assert "Tree-of-Thought Report" in report
        assert "INC-001" in report
        assert "Hypothesis Scores" in report
        assert "Plan-and-Solve Validation" in report

    def test_tot_result_requires_minimum_fields(self):
        """TreeOfThoughtResult must enforce minimum field requirements."""
        diagnosis = _make_diagnosis()
        agent = TreeOfThoughtAgent()
        result = agent.run(diagnosis)
        with pytest.raises(Exception):
            TreeOfThoughtResult(
                agent_type="tree_of_thought",
                incident_id="INC-001",
                hypotheses=[],
                selected_hypothesis=result.selected_hypothesis,
                selected_hypothesis_index=0,
                reasoning_steps=[],
                selected_evidence=[],
                plan_and_solve_validation="too short",
                accuracy_improvement=0.1,
                confidence=0.8,
                risk_tier=diagnosis.risk_tier,
                final_diagnosis=diagnosis,
                recommendation="too short",
                hypothesis_scores={},
            )


# ---------------------------------------------------------------------------
# TreeOfThoughtAgent — generate hypotheses
# ---------------------------------------------------------------------------


class TestTreeOfThoughtAgentGenerate:
    def test_generates_minimum_hypotheses(self):
        """TreeOfThoughtAgent should generate at least 2 hypotheses."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        assert len(hypotheses) >= 2

    def test_all_hypotheses_are_valid(self):
        """All generated hypotheses must be valid Hypothesis objects."""
        agent = TreeOfThoughtAgent(num_hypotheses=4)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        for hyp in hypotheses:
            assert isinstance(hyp, Hypothesis)
            Hypothesis.model_validate(hyp.model_dump())

    def test_hypotheses_have_unique_ids(self):
        """Each hypothesis should have a unique hypothesis_id."""
        agent = TreeOfThoughtAgent(num_hypotheses=4)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        ids = [h.hypothesis_id for h in hypotheses]
        assert len(ids) == len(set(ids))

    def test_first_hypothesis_is_baseline(self):
        """The first hypothesis should represent the original diagnosis."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis(confidence=0.85)
        hypotheses = agent.generate_hypotheses(diagnosis)
        first = hypotheses[0]
        assert first.root_cause == diagnosis.root_cause

    def test_hypotheses_include_alternatives(self):
        """Generated hypotheses should include alternative scenarios."""
        agent = TreeOfThoughtAgent(num_hypotheses=4)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        # Should have at least one alternative (not just baseline)
        assert len(hypotheses) >= 2
        # Alternative hypotheses should have different scenarios
        scenarios = [h.scenario for h in hypotheses]
        assert len(set(scenarios)) >= 2


# ---------------------------------------------------------------------------
# TreeOfThoughtAgent — evaluate hypotheses
# ---------------------------------------------------------------------------


class TestTreeOfThoughtAgentEvaluate:
    def test_evaluate_returns_scores_for_all_hypotheses(self):
        """evaluate_hypotheses should return a score for each hypothesis."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        scores = agent.evaluate_hypotheses(hypotheses)
        assert len(scores) == len(hypotheses)

    def test_scores_are_between_0_and_1(self):
        """All scores must be between 0.0 and 1.0."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        scores = agent.evaluate_hypotheses(hypotheses)
        for score in scores.values():
            assert 0.0 <= score <= 1.0

    def test_scores_have_valid_mapping(self):
        """Scores dict should map hypothesis_id to float."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        scores = agent.evaluate_hypotheses(hypotheses)
        for h_id, score in scores.items():
            assert isinstance(h_id, str)
            assert isinstance(score, float)

    def test_higher_confidence_gets_higher_score(self):
        """Hypotheses with higher confidence should generally get higher scores."""
        agent = TreeOfThoughtAgent(num_hypotheses=2)
        diagnosis_high = _make_diagnosis(confidence=0.9)
        diagnosis_low = _make_diagnosis(confidence=0.5)
        hyp_high = agent.generate_hypotheses(diagnosis_high)
        hyp_low = agent.generate_hypotheses(diagnosis_low)
        scores_high = agent.evaluate_hypotheses(hyp_high)
        scores_low = agent.evaluate_hypotheses(hyp_low)
        # The baseline hypothesis should score higher with higher base confidence
        base_high = scores_high[hyp_high[0].hypothesis_id]
        base_low = scores_low[hyp_low[0].hypothesis_id]
        assert base_high >= base_low


# ---------------------------------------------------------------------------
# TreeOfThoughtAgent — select best hypothesis
# ---------------------------------------------------------------------------


class TestTreeOfThoughtAgentSelect:
    def test_select_returns_best_hypothesis(self):
        """select_best_hypothesis should return the highest-scoring hypothesis."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        scores = agent.evaluate_hypotheses(hypotheses)
        best, idx = agent.select_best_hypothesis(hypotheses, scores)
        # Verify it's the one with the max score
        max_id = max(scores, key=scores.get)
        assert best.hypothesis_id == max_id
        assert idx == next(i for i, h in enumerate(hypotheses) if h.hypothesis_id == max_id)

    def test_selected_index_is_valid(self):
        """Selected hypothesis index should be within bounds."""
        agent = TreeOfThoughtAgent(num_hypotheses=3)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        scores = agent.evaluate_hypotheses(hypotheses)
        best, idx = agent.select_best_hypothesis(hypotheses, scores)
        assert 0 <= idx < len(hypotheses)
        assert best is hypotheses[idx]

    def test_all_hypotheses_have_scores(self):
        """Every hypothesis should have an entry in scores."""
        agent = TreeOfThoughtAgent(num_hypotheses=4)
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        scores = agent.evaluate_hypotheses(hypotheses)
        for hyp in hypotheses:
            assert hyp.hypothesis_id in scores


# ---------------------------------------------------------------------------
# TreeOfThoughtAgent — plan and solve
# ---------------------------------------------------------------------------


class TestPlanAndSolve:
    def test_plan_and_solve_returns_string(self):
        """plan_and_solve should return a validation description string."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        best = hypotheses[0]
        validation = agent.plan_and_solve(best, diagnosis)
        assert isinstance(validation, str)
        assert len(validation) >= 10

    def test_plan_and_solve_mentions_hypothesis(self):
        """Validation description should reference the hypothesis."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        hypotheses = agent.generate_hypotheses(diagnosis)
        best = hypotheses[0]
        validation = agent.plan_and_solve(best, diagnosis)
        assert best.hypothesis_id in validation

    def test_plan_and_solve_mentions_evidence(self):
        """Validation description should reference evidence items."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis(evidence_count=3)
        hypotheses = agent.generate_hypotheses(diagnosis)
        best = hypotheses[0]
        validation = agent.plan_and_solve(best, diagnosis)
        assert "evidence" in validation.lower()


# ---------------------------------------------------------------------------
# TreeOfThoughtAgent — run
# ---------------------------------------------------------------------------


class TestTreeOfThoughtAgentRun:
    @pytest.fixture(autouse=True)
    def agent(self):
        return TreeOfThoughtAgent(num_hypotheses=4, random_seed=42)

    def test_run_returns_tot_result(self, agent):
        """run() must return a TreeOfThoughtResult."""
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        assert isinstance(result, TreeOfThoughtResult)
        TreeOfThoughtResult.model_validate(result.model_dump())

    def test_run_has_all_fields(self, agent):
        """TreeOfThoughtResult from run() must have all required fields."""
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        assert result.incident_id == "INC-001"
        assert len(result.hypotheses) >= 2
        assert len(result.reasoning_steps) >= 1
        assert len(result.selected_evidence) >= 1

    def test_run_agent_type_is_tree_of_thought(self, agent):
        """TreeOfThoughtResult agent_type must be tree_of_thought."""
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        assert result.agent_type == "tree_of_thought"

    def test_run_contains_original_diagnosis(self, agent):
        """TreeOfThoughtResult must contain the final Diagnosis."""
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        assert isinstance(result.final_diagnosis, Diagnosis)
        assert result.final_diagnosis.incident_id == "INC-001"

    def test_run_selects_best_hypothesis(self, agent):
        """The selected hypothesis should be the one with the highest score."""
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        best_score = max(result.hypothesis_scores.values())
        assert result.hypothesis_scores[result.selected_hypothesis.hypothesis_id] == best_score

    def test_run_produces_valid_schema(self, agent):
        """TreeOfThoughtResult must pass Pydantic model validation after run()."""
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        TreeOfThoughtResult.model_validate(result.model_dump())

    def test_run_does_not_break_original_diagnosis(self, agent):
        """The original diagnosis should remain unchanged after ToT."""
        diagnosis = _make_diagnosis(confidence=0.85)
        original_confidence = diagnosis.confidence
        result = agent.run(diagnosis)
        assert result.final_diagnosis.confidence >= original_confidence

    def test_run_with_fewer_hypotheses(self, agent):
        """run() with num_hypotheses=2 should have 2 hypotheses."""
        agent2 = TreeOfThoughtAgent(num_hypotheses=2)
        diagnosis = _make_diagnosis()
        result = agent2.run(diagnosis)
        assert len(result.hypotheses) == 2

    def test_run_with_many_hypotheses(self, agent):
        """run() with num_hypotheses=6 should have 6 hypotheses."""
        agent6 = TreeOfThoughtAgent(num_hypotheses=6)
        diagnosis = _make_diagnosis()
        result = agent6.run(diagnosis)
        assert len(result.hypotheses) == 6


# ---------------------------------------------------------------------------
# Accuracy improvement
# ---------------------------------------------------------------------------


class TestAccuracyImprovement:
    def test_accuracy_improvement_is_non_negative(self):
        """Accuracy improvement should be non-negative."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        assert result.accuracy_improvement >= 0.0

    def test_accuracy_improvement_is_between_0_and_1(self):
        """Accuracy improvement must be between 0.0 and 1.0."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        assert 0.0 <= result.accuracy_improvement <= 1.0

    def test_multiple_runs_are_deterministic(self):
        """Multiple runs with the same seed should produce consistent results."""
        agent = TreeOfThoughtAgent(num_hypotheses=4, random_seed=42)
        diagnosis = _make_diagnosis()
        result1 = agent.run(diagnosis)
        result2 = agent.run(diagnosis)
        assert result1.accuracy_improvement == result2.accuracy_improvement
        assert len(result1.hypotheses) == len(result2.hypotheses)
        assert result1.selected_hypothesis_index == result2.selected_hypothesis_index


# ---------------------------------------------------------------------------
# Cross-cutting: schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Verify all Tree-of-Thought outputs are validated Pydantic objects."""

    def test_tot_result_schema_valid_after_run(self):
        """TreeOfThoughtResult must be schema-valid after run()."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        TreeOfThoughtResult.model_validate(result.model_dump())

    def test_hypotheses_schema_valid_in_result(self):
        """All hypotheses inside TreeOfThoughtResult must be valid."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        for hyp in result.hypotheses:
            Hypothesis.model_validate(hyp.model_dump())

    def test_selected_hypothesis_schema_valid(self):
        """Selected hypothesis must be valid."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        Hypothesis.model_validate(result.selected_hypothesis.model_dump())

    def test_final_diagnosis_schema_valid(self):
        """Final diagnosis inside TreeOfThoughtResult must be valid."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        Diagnosis.model_validate(result.final_diagnosis.model_dump())

    def test_selected_evidence_schema_valid(self):
        """Selected evidence items must be valid EvidenceItems."""
        agent = TreeOfThoughtAgent()
        diagnosis = _make_diagnosis()
        result = agent.run(diagnosis)
        for ev in result.selected_evidence:
            EvidenceItem.model_validate(ev.model_dump())


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify existing agents still work after Stage 9 addition."""

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

    def test_debate_mechanism_still_works(self):
        """DebateMechanism should still produce valid DebateOutcomes."""
        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = orchestrator.run("INC-001")
        mechanism = DebateMechanism()
        outcome = mechanism.run(diagnosis)
        DebateOutcome.model_validate(outcome.model_dump())

    def test_root_cause_agent_still_works(self):
        """RootCauseAgent should still produce valid arguments."""
        agent = RootCauseAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        arguments = agent.analyze(diagnosis)
        for arg in arguments:
            Argument.model_validate(arg.model_dump())

    def test_forensic_examiner_still_works(self):
        """ForensicExaminerAgent should still produce valid challenges."""
        agent = ForensicExaminerAgent()
        diagnosis = _make_diagnosis(confidence=0.85)
        challenges = agent.challenge(diagnosis)
        for chal in challenges:
            Argument.model_validate(chal.model_dump())

    def test_tot_does_not_break_existing_agents(self):
        """Adding Tree-of-Thought should not break existing agents."""
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

        mechanism = DebateMechanism()
        debate_outcome = mechanism.run(orch_diagnosis)
        DebateOutcome.model_validate(debate_outcome.model_dump())

    def test_all_agents_produce_valid_outputs(self):
        """All agents produce valid Pydantic outputs after Stage 9."""
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

            tot_agent = TreeOfThoughtAgent(num_hypotheses=4)
            tot_result = tot_agent.run(orch_diag)
            TreeOfThoughtResult.model_validate(tot_result.model_dump())
