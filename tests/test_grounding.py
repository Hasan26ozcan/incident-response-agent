"""Shared grounding helpers (used by every LLM agent)."""

from __future__ import annotations

from incident_agent.agents.grounding import degraded_diagnosis, ground_diagnosis, is_grounded, tokens
from incident_agent.agents.llm_react_agent import LLMDiagnosis
from incident_agent.schemas.agent_output import RiskTier
from tests.fakes import FINAL


def _out(**over):
    return LLMDiagnosis.model_validate({**FINAL, **over})


def test_tokens_and_grounding_threshold():
    assert "retry" in tokens("Retry loop!") and is_grounded("retry loop", tokens("a retry loop here"))
    assert not is_grounded("kernel panic zzqx", tokens("a retry loop here")) and not is_grounded("", set())


def test_confidence_cap_applies_after_penalties():
    d = ground_diagnosis("t", "INC-001", _out(confidence=0.9), "retry loop", confidence_cap=0.5)
    assert d.confidence == 0.5


def test_notes_are_appended_as_reasoning_steps():
    d = ground_diagnosis("t", "INC-001", _out(), "retry loop", notes=["one note"])
    assert d.reasoning_steps[-1].description == "one note"


def test_degraded_diagnosis_for_unknown_incident_uses_placeholder_service():
    d = degraded_diagnosis("t", "INC-999", "", "why", 0)
    assert d.affected_service == "unknown" and d.risk_tier is RiskTier.HIGH and d.evidence[0].detail
