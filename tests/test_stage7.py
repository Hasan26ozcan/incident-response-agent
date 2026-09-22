"""Tests for Stage 7 — Hierarchical Swarm.

Covers:
  - CommanderDiagnosis schema validation.
  - IncidentCommander synthesizes narratives.
  - IncidentCommander determines escalation decisions.
  - IncidentCommander assesses severity.
  - IncidentCommander.run() produces validated CommanderDiagnosis.
  - CommanderDiagnosis contains the underlying Diagnosis.
  - CommanderDiagnosis contains all WorkerFinding objects.
  - Cross-cutting rule: CommanderDiagnosis is a validated Pydantic object.
  - Backward compatibility: ReActAgent and OrchestratorAgent still work.
  - End-to-end diagnosis flow: Orchestrator → Commander.
"""

from __future__ import annotations

import pytest

from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import OrchestratorAgent
from incident_agent.agents.react_agent import ReActAgent
from incident_agent.schemas import CommanderDiagnosis, Diagnosis, WorkerFinding
from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep
from incident_agent.schemas.diagnosis import Diagnosis as DiagnosisSchema


# ---------------------------------------------------------------------------
# Helper fixtures
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
) -> DiagnosisSchema:
    return DiagnosisSchema(
        agent_type="orchestrator_worker",
        incident_id="INC-001",
        root_cause=root_cause,
        confidence=confidence,
        evidence=_default_evidence(),
        affected_service="test-service",
        category=category,
        reasoning_steps=_default_reasoning(),
        recommendation="Test recommendation for testing purposes that is long enough",
        risk_tier=risk_tier,
    )


def _make_worker_findings() -> dict:
    return {
        "log": WorkerFinding(
            worker_type="log",
            incident_id="INC-001",
            evidence=_default_evidence(),
            reasoning_steps=_default_reasoning(),
            confidence=0.8,
            summary="Log analysis summary for testing that is long enough",
        ),
        "metrics": WorkerFinding(
            worker_type="metrics",
            incident_id="INC-001",
            evidence=[
                EvidenceItem(
                    source_type=EvidenceType.METRIC_ANOMALY,
                    source="metric:cpu",
                    detail="CPU anomaly detected",
                    confidence_weight=0.85,
                )
            ],
            reasoning_steps=_default_reasoning(),
            confidence=0.75,
            summary="Metrics analysis summary for testing that is long enough",
        ),
    }


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
# CommanderDiagnosis schema
# ---------------------------------------------------------------------------


class TestCommanderDiagnosisSchema:
    """Verify CommanderDiagnosis is a properly structured Pydantic model."""

    def test_commander_diagnosis_is_pydantic_model(self):
        """CommanderDiagnosis should be a Pydantic BaseModel subclass."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert isinstance(cmd_diag, CommanderDiagnosis)

    def test_commander_diagnosis_inherits_agent_output(self):
        """CommanderDiagnosis should inherit from AgentOutput."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.agent_type == "incident_commander"
        assert cmd_diag.incident_id == "INC-001"
        assert 0.0 <= cmd_diag.confidence <= 1.0

    def test_commander_diagnosis_has_narrative(self):
        """CommanderDiagnosis must have a narrative."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert len(cmd_diag.narrative) >= 20
        assert "INC-001" in cmd_diag.narrative

    def test_commander_diagnosis_has_escalation_decision(self):
        """CommanderDiagnosis must have a valid escalation decision."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.escalation_decision in ("escalate", "monitor", "resolve")

    def test_commander_diagnosis_has_severity(self):
        """CommanderDiagnosis must have a severity assessment."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert len(cmd_diag.severity_assessment) >= 10

    def test_commander_diagnosis_contains_diagnosis(self):
        """CommanderDiagnosis must contain the underlying Diagnosis."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert isinstance(cmd_diag.diagnosis, DiagnosisSchema)
        assert cmd_diag.diagnosis.incident_id == "INC-001"

    def test_commander_diagnosis_contains_worker_findings(self):
        """CommanderDiagnosis must contain all worker findings."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert len(cmd_diag.worker_findings) >= 1
        worker_types = {wf.worker_type for wf in cmd_diag.worker_findings}
        assert "log" in worker_types
        assert "metrics" in worker_types

    def test_commander_diagnosis_has_evidence(self):
        """CommanderDiagnosis must have at least one evidence item."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert len(cmd_diag.evidence) >= 1

    def test_commander_diagnosis_has_reasoning_steps(self):
        """CommanderDiagnosis must have at least one reasoning step."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert len(cmd_diag.reasoning_steps) >= 1

    def test_commander_diagnosis_schema_valid(self):
        """CommanderDiagnosis must pass Pydantic model validation."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())

    def test_commander_diagnosis_to_json_roundtrip(self):
        """CommanderDiagnosis.to_json() and from_json() must roundtrip."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        json_str = cmd_diag.to_json()
        restored = CommanderDiagnosis.from_json(json_str)
        assert restored.incident_id == cmd_diag.incident_id
        assert restored.escalation_decision == cmd_diag.escalation_decision
        assert restored.confidence == cmd_diag.confidence

    def test_commander_diagnosis_format_report(self):
        """CommanderDiagnosis.format_report() must produce a readable report."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        report = cmd_diag.format_report()
        assert "Incident Commander Report" in report
        assert "INC-001" in report
        assert "Narrative" in report

    def test_commander_diagnosis_requires_minimum_fields(self):
        """CommanderDiagnosis must enforce minimum field requirements."""
        with pytest.raises(Exception):
            CommanderDiagnosis(
                agent_type="incident_commander",
                incident_id="INC-001",
                narrative="too short",
                escalation_decision="escalate",
                severity_assessment="too short",
                confidence=0.5,
                risk_tier="low",
                diagnosis=_make_diagnosis(),
                worker_findings=[],
                evidence=[],
                affected_service="test",
                category="test",
                recommendation="too short",
                reasoning_steps=[],
            )

    def test_commander_diagnosis_escalation_validation(self):
        """CommanderDiagnosis must validate escalation decision."""
        diagnosis = _make_diagnosis()
        findings = _make_worker_findings()
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.escalation_decision in ("escalate", "monitor", "resolve")


# ---------------------------------------------------------------------------
# IncidentCommander — synthesize_narrative
# ---------------------------------------------------------------------------


class TestIncidentCommanderSynthesizeNarrative:
    def test_narrative_contains_incident_id(self):
        """Narrative must reference the incident ID."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        narrative = commander.synthesize_narrative(diagnosis, findings, "INC-001")
        assert "INC-001" in narrative

    def test_narrative_contains_root_cause(self):
        """Narrative must reference the root cause."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        narrative = commander.synthesize_narrative(diagnosis, findings, "INC-001")
        assert "Test root cause" in narrative

    def test_narrative_contains_worker_summaries(self):
        """Narrative must include worker summaries."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        narrative = commander.synthesize_narrative(diagnosis, findings, "INC-001")
        assert "log analysis summary" in narrative

    def test_narrative_is_string(self):
        """Narrative must be a string."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        narrative = commander.synthesize_narrative(diagnosis, findings, "INC-001")
        assert isinstance(narrative, str)
        assert len(narrative) >= 20

    def test_narrative_with_empty_findings(self):
        """Narrative should handle empty findings gracefully."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        narrative = commander.synthesize_narrative(diagnosis, {}, "INC-001")
        assert isinstance(narrative, str)
        assert len(narrative) >= 20
        assert "No specialist worker findings" in narrative


# ---------------------------------------------------------------------------
# IncidentCommander — determine_escalation
# ---------------------------------------------------------------------------


class TestIncidentCommanderEscalation:
    def test_high_risk_escalates(self):
        """High-risk incidents must escalate."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="high", confidence=0.9)
        findings = {"log": _make_worker_finding()}
        decision = commander.determine_escalation(diagnosis, findings)
        assert decision == "escalate"

    def test_medium_risk_low_confidence_escalates(self):
        """Medium-risk with low confidence must escalate."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="medium", confidence=0.5)
        findings = {"log": _make_worker_finding()}
        decision = commander.determine_escalation(diagnosis, findings)
        assert decision == "escalate"

    def test_medium_risk_high_confidence_monitors(self):
        """Medium-risk with good confidence should monitor."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="medium", confidence=0.8)
        findings = {"log": _make_worker_finding()}
        decision = commander.determine_escalation(diagnosis, findings)
        assert decision == "monitor"

    def test_low_risk_resolves(self):
        """Low-risk with good confidence should resolve."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="low", confidence=0.8)
        findings = {"log": _make_worker_finding()}
        decision = commander.determine_escalation(diagnosis, findings)
        assert decision == "resolve"

    def test_low_risk_multiple_workers_monitors(self):
        """Low-risk with multiple workers should monitor."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="low", confidence=0.5)
        findings = {
            "log": _make_worker_finding("log"),
            "metrics": _make_worker_finding("metrics"),
            "deploy": _make_worker_finding("deploy"),
        }
        decision = commander.determine_escalation(diagnosis, findings)
        assert decision == "monitor"

    def test_escalation_returns_valid_string(self):
        """Escalation decision must be a valid string."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="high", confidence=0.9)
        findings = {"log": _make_worker_finding()}
        decision = commander.determine_escalation(diagnosis, findings)
        assert decision in ("escalate", "monitor", "resolve")


# ---------------------------------------------------------------------------
# IncidentCommander — assess_severity
# ---------------------------------------------------------------------------


class TestIncidentCommanderSeverity:
    def test_high_risk_is_critical(self):
        """High-risk incidents should be assessed as Critical."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="high", confidence=0.9)
        findings = {}
        severity = commander.assess_severity(diagnosis, findings)
        assert "Critical" in severity

    def test_medium_risk_is_elevated(self):
        """Medium-risk incidents should be assessed as Elevated."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="medium", confidence=0.8)
        findings = {}
        severity = commander.assess_severity(diagnosis, findings)
        assert "Elevated" in severity

    def test_low_risk_is_low(self):
        """Low-risk incidents should be assessed as Low."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="low", confidence=0.8)
        findings = {}
        severity = commander.assess_severity(diagnosis, findings)
        assert "Low" in severity

    def test_severity_is_string(self):
        """Severity must be a string."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="high", confidence=0.9)
        findings = {}
        severity = commander.assess_severity(diagnosis, findings)
        assert isinstance(severity, str)
        assert len(severity) >= 10


# ---------------------------------------------------------------------------
# IncidentCommander — build_reasoning
# ---------------------------------------------------------------------------


class TestIncidentCommanderReasoning:
    def test_reasoning_steps_are_reasoning_step_objects(self):
        """Reasoning steps must be ReasoningStep objects."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        narrative = commander.synthesize_narrative(diagnosis, findings, "INC-001")
        steps = commander.build_reasoning(diagnosis, findings, narrative)
        assert len(steps) >= 1
        for step in steps:
            assert isinstance(step, ReasoningStep)
            assert step.step_number >= 1
            assert len(step.description) >= 1

    def test_reasoning_steps_sequential(self):
        """Reasoning step numbers should be sequential."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        narrative = commander.synthesize_narrative(diagnosis, findings, "INC-001")
        steps = commander.build_reasoning(diagnosis, findings, narrative)
        for i, step in enumerate(steps, 1):
            assert step.step_number == i


# ---------------------------------------------------------------------------
# IncidentCommander — run
# ---------------------------------------------------------------------------


class TestIncidentCommanderRun:
    @pytest.fixture(autouse=True)
    def commander(self):
        return IncidentCommander(confidence_threshold=0.7)

    def test_run_returns_commander_diagnosis(self, commander):
        """run() must return a CommanderDiagnosis."""
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert isinstance(cmd_diag, CommanderDiagnosis)
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())

    def test_run_has_all_fields(self, commander):
        """CommanderDiagnosis from run() must have all required fields."""
        diagnosis = _make_diagnosis()
        findings = {"log": _make_worker_finding("log")}
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.incident_id == "INC-001"
        assert cmd_diag.narrative
        assert cmd_diag.escalation_decision
        assert cmd_diag.severity_assessment
        assert cmd_diag.confidence > 0
        assert cmd_diag.risk_tier
        assert cmd_diag.diagnosis is not None
        assert cmd_diag.worker_findings

    def test_run_agent_type_is_incident_commander(self, commander):
        """CommanderDiagnosis agent_type must be incident_commander."""
        diagnosis = _make_diagnosis(risk_tier="low")
        findings = {"log": _make_worker_finding("log")}
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.agent_type == "incident_commander"

    def test_run_commander_confidence(self, commander):
        """Commander confidence should be slightly higher than diagnosis."""
        diagnosis = _make_diagnosis(confidence=0.8)
        findings = {"log": _make_worker_finding("log")}
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.confidence >= diagnosis.confidence
        assert cmd_diag.confidence <= 0.95

    def test_run_with_empty_findings(self, commander):
        """run() should handle empty worker findings gracefully."""
        diagnosis = _make_diagnosis(risk_tier="low")
        cmd_diag = commander.run(diagnosis, {}, "INC-001")
        assert isinstance(cmd_diag, CommanderDiagnosis)
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())

    def test_run_with_multiple_worker_findings(self, commander):
        """run() should handle multiple worker findings."""
        diagnosis = _make_diagnosis()
        findings = {
            "log": _make_worker_finding("log"),
            "metrics": _make_worker_finding("metrics"),
            "deploy": _make_worker_finding("deploy"),
        }
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert len(cmd_diag.worker_findings) == 3


# ---------------------------------------------------------------------------
# IncidentCommander — build_evidence
# ---------------------------------------------------------------------------


class TestIncidentCommanderEvidence:
    def test_build_evidence_aggregates_diagnosis_evidence(self):
        """build_evidence should include diagnosis evidence."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {}
        evidence = commander.build_evidence(diagnosis, findings)
        assert len(evidence) >= 1
        assert any("Test evidence" in e.detail for e in evidence)

    def test_build_evidence_includes_worker_evidence(self):
        """build_evidence should include evidence from workers."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis()
        findings = {
            "log": WorkerFinding(
                worker_type="log", incident_id="INC-001",
                evidence=[
                    EvidenceItem(
                        source_type=EvidenceType.LOG_ENTRY,
                        source="log:worker",
                        detail="Worker evidence",
                        confidence_weight=0.8,
                    )
                ],
                reasoning_steps=_default_reasoning(),
                confidence=0.8,
                summary="Log worker summary for testing that is long enough",
            ),
        }
        evidence = commander.build_evidence(diagnosis, findings)
        assert any("Worker evidence" in e.detail for e in evidence)


# ---------------------------------------------------------------------------
# IncidentCommander — build_recommendation
# ---------------------------------------------------------------------------


class TestIncidentCommanderRecommendation:
    def test_escalate_recommendation_contains_escalate(self):
        """Escalate recommendation must mention escalation."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="high")
        rec = commander.build_recommendation(diagnosis, "escalate")
        assert "IMMEDIATE ESCALATION" in rec

    def test_monitor_recommendation_contains_monitor(self):
        """Monitor recommendation must mention monitoring."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="medium")
        rec = commander.build_recommendation(diagnosis, "monitor")
        assert "CONTINUED MONITORING" in rec

    def test_resolve_recommendation_contains_resolve(self):
        """Resolve recommendation must mention resolution."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="low")
        rec = commander.build_recommendation(diagnosis, "resolve")
        assert "RESOLUTION PATH" in rec


# ---------------------------------------------------------------------------
# End-to-end: Orchestrator → Incident Commander
# ---------------------------------------------------------------------------


class TestEndToEndOrchestratorToCommander:
    """Verify the full end-to-end flow: Orchestrator produces Diagnosis,
    Incident Commander produces CommanderDiagnosis."""

    def test_orchestrator_then_commander(self):
        """Full pipeline: Orchestrator → Commander should work."""
        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = orchestrator.run("INC-001")
        assert isinstance(diagnosis, DiagnosisSchema)
        DiagnosisSchema.model_validate(diagnosis.model_dump())

        commander = IncidentCommander(confidence_threshold=0.7)
        findings = {
            "log": WorkerFinding(
                worker_type="log", incident_id="INC-001",
                evidence=diagnosis.evidence[:1] if diagnosis.evidence else _default_evidence(),
                reasoning_steps=diagnosis.reasoning_steps[:1] if diagnosis.reasoning_steps else _default_reasoning(),
                confidence=diagnosis.confidence,
                summary="Orchestrator log worker summary for testing that is long enough",
            ),
            "metrics": WorkerFinding(
                worker_type="metrics", incident_id="INC-001",
                evidence=_default_evidence(),
                reasoning_steps=_default_reasoning(),
                confidence=diagnosis.confidence * 0.9,
                summary="Orchestrator metrics worker summary for testing that is long enough",
            ),
        }
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert isinstance(cmd_diag, CommanderDiagnosis)
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())
        assert cmd_diag.diagnosis.incident_id == "INC-001"

    def test_commander_diagnosis_contains_full_diagnosis(self):
        """CommanderDiagnosis must contain the complete Diagnosis object."""
        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = orchestrator.run("INC-001")
        findings = {
            "log": WorkerFinding(
                worker_type="log", incident_id="INC-001",
                evidence=diagnosis.evidence[:1] if diagnosis.evidence else _default_evidence(),
                reasoning_steps=diagnosis.reasoning_steps[:1] if diagnosis.reasoning_steps else _default_reasoning(),
                confidence=diagnosis.confidence,
                summary="Orchestrator log worker summary for testing that is long enough",
            ),
        }
        commander = IncidentCommander(confidence_threshold=0.7)
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        assert cmd_diag.diagnosis.root_cause == diagnosis.root_cause
        assert cmd_diag.diagnosis.confidence == diagnosis.confidence
        assert cmd_diag.diagnosis.category == diagnosis.category

    def test_both_agents_produce_valid_outputs(self):
        """Both OrchestratorAgent and IncidentCommander produce valid Pydantic outputs."""
        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = orchestrator.run("INC-002")
        DiagnosisSchema.model_validate(diagnosis.model_dump())

        commander = IncidentCommander(confidence_threshold=0.7)
        findings = {
            "log": WorkerFinding(
                worker_type="log", incident_id="INC-002",
                evidence=diagnosis.evidence[:1] if diagnosis.evidence else _default_evidence(),
                reasoning_steps=diagnosis.reasoning_steps[:1] if diagnosis.reasoning_steps else _default_reasoning(),
                confidence=diagnosis.confidence,
                summary="Test worker summary for testing that is long enough",
            ),
        }
        cmd_diag = commander.run(diagnosis, findings, "INC-002")
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify ReActAgent and OrchestratorAgent still work alongside Commander."""

    def test_react_agent_still_works(self):
        """ReActAgent should still produce valid diagnoses."""
        agent = ReActAgent(confidence_threshold=0.5)
        diagnosis = agent.run("INC-001")
        DiagnosisSchema.model_validate(diagnosis.model_dump())
        assert diagnosis.agent_type == "single_agent_react"

    def test_orchestrator_still_works(self):
        """OrchestratorAgent should still produce valid diagnoses."""
        agent = OrchestratorAgent(confidence_threshold=0.5)
        diagnosis = agent.run("INC-001")
        DiagnosisSchema.model_validate(diagnosis.model_dump())
        assert diagnosis.agent_type == "orchestrator_worker"

    def test_commander_does_not_break_existing_agents(self):
        """Adding Commander should not break existing agents."""
        react = ReActAgent(confidence_threshold=0.5)
        react_diagnosis = react.run("INC-003")
        DiagnosisSchema.model_validate(react_diagnosis.model_dump())

        orchestrator = OrchestratorAgent(confidence_threshold=0.5)
        orch_diagnosis = orchestrator.run("INC-003")
        DiagnosisSchema.model_validate(orch_diagnosis.model_dump())

        commander = IncidentCommander(confidence_threshold=0.7)
        findings = {
            "log": WorkerFinding(
                worker_type="log", incident_id="INC-003",
                evidence=orch_diagnosis.evidence[:1] if orch_diagnosis.evidence else _default_evidence(),
                reasoning_steps=orch_diagnosis.reasoning_steps[:1] if orch_diagnosis.reasoning_steps else _default_reasoning(),
                confidence=orch_diagnosis.confidence,
                summary="Test worker summary for testing that is long enough",
            ),
        }
        cmd_diag = commander.run(orch_diagnosis, findings, "INC-003")
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())


# ---------------------------------------------------------------------------
# Cross-cutting: schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Verify CommanderDiagnosis is always a validated Pydantic object."""

    def test_commander_diagnosis_schema_valid_after_run(self):
        """CommanderDiagnosis must be schema-valid after run()."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="low")
        findings = {"log": _make_worker_finding("log")}
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        CommanderDiagnosis.model_validate(cmd_diag.model_dump())

    def test_worker_finding_schema_valid_in_commander(self):
        """WorkerFindings inside CommanderDiagnosis must be valid."""
        commander = IncidentCommander(confidence_threshold=0.7)
        diagnosis = _make_diagnosis(risk_tier="low")
        findings = {"log": _make_worker_finding("log")}
        cmd_diag = commander.run(diagnosis, findings, "INC-001")
        for wf in cmd_diag.worker_findings:
            WorkerFinding.model_validate(wf.model_dump())
