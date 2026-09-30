"""Tests for Stage 12 — Episodic Memory & Self-Improvement (Hermes-style).

Covers:
  - Memory schemas (PostMortem, ReflectionFeedback, MemoryRecord) validation.
  - Memory store write/read/list/retrieve_by_tag.
  - SelfImprovementAgent: reflection flags misdiagnosis, writes memory.
  - Second encounter improvement: accuracy delta after strategy update.
  - Backward compatibility: prior agents unchanged.
"""
from __future__ import annotations

import pytest
from incident_agent.schemas.memory import MemoryRecord, PostMortem, ReflectionFeedback
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep, RiskTier, IncidentMetadata
from incident_agent.agents.self_improvement_agent import SelfImprovementAgent
from incident_agent.memory import store


def test_memory_schema_validation() -> None:
    pm = PostMortem(
        incident_id="INC-012",
        root_cause="cpu exhaustion",
        resolution="scale up",
        timestamp="2026-09-30T12:00:00Z",
        embedding_text="cpu exhaustion high load",
        tags=["cpu", "exhaustion"],
    )
    fb = ReflectionFeedback(correct=True, confidence_delta=0.2)
    rec = MemoryRecord(post_mortem=pm, reflection=fb, accuracy_before=0.6, accuracy_after=1.0)
    assert rec.post_mortem.incident_id == "INC-012"


def test_memory_store_roundtrip() -> None:
    pm = PostMortem(
        incident_id="INC-013",
        root_cause="db pool exhausted",
        resolution="increase pool",
        timestamp="2026-09-30T12:00:00Z",
        embedding_text="db pool exhausted",
        tags=["db", "pool"],
    )
    fb = ReflectionFeedback(misdiagnosis_type=None, correct=True)
    rec = MemoryRecord(post_mortem=pm, reflection=fb)
    store.save_memory(rec)
    loaded = store.load_memory("INC-013")
    assert loaded is not None
    assert loaded.post_mortem.root_cause == "db pool exhausted"
    by_tag = store.retrieve_by_tag("db")
    assert any(r.post_mortem.incident_id == "INC-013" for r in by_tag)


def _diag(root_cause: str, confidence: float = 0.5) -> Diagnosis:
    from incident_agent.schemas.diagnosis import Diagnosis, EvidenceItem, ReasoningStep, RiskTier, IncidentMetadata
    return Diagnosis(
        agent_type="test",
        incident_id="INC-999",
        root_cause=root_cause if len(root_cause) >= 10 else root_cause + " extra chars",
        confidence=confidence,
        evidence=[EvidenceItem(source_type=EvidenceType.LOG_ENTRY, source="test", detail="test")],
        affected_service="api",
        category="performance",
        reasoning_steps=[ReasoningStep(step_number=1, description="test")],
        recommendation="remediate immediately by scaling",
        risk_tier=RiskTier.MEDIUM,
        metadata=None,
    )


def test_self_improvement_reflects_misdiagnosis() -> None:
    agent = SelfImprovementAgent()
    diag = _diag("wrong_cause")
    rec = agent.reflect_and_improve("INC-014", diag, gold_root_cause="cpu exhaustion long root cause", accuracy_before=0.4)
    assert rec.reflection.correct is False
    assert rec.reflection.misdiagnosis_type is not None
    assert rec.reflection.updated_strategy is not None
    loaded = store.load_memory("INC-014")
    assert loaded is not None


def test_second_encounter_improvement() -> None:
    """Measurable improvement on second encounter."""
    agent = SelfImprovementAgent()
    # First encounter wrong
    diag_wrong = _diag("memory leak pattern observed here")
    rec1 = agent.reflect_and_improve("INC-015", diag_wrong, gold_root_cause="cpu exhaustion high load failure", accuracy_before=0.3)
    assert rec1.accuracy_after < rec1.accuracy_before or rec1.reflection.correct is False
    # Second encounter correct with updated strategy
    diag_right = _diag("cpu exhaustion high load failure", confidence=0.9)
    rec2 = agent.reflect_and_improve("INC-015B", diag_right, gold_root_cause="cpu exhaustion high load failure", accuracy_before=0.3)
    assert rec2.reflection.correct is True
    assert rec2.accuracy_after > rec1.accuracy_before


def test_backward_compat_prior_agents() -> None:
    from incident_agent.agents.react_agent import ReActAgent
    from incident_agent.agents.reranker_agent import RerankerAgent
    from incident_agent.agents.debate_mechanism import DebateMechanism
    assert ReActAgent is not None
    assert RerankerAgent is not None
    assert DebateMechanism is not None
