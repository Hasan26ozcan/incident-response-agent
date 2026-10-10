"""Shared helpers for turning an LLM answer into a validated, grounded ``Diagnosis``.

Used by every LLM-driven agent (single ReAct agent, orchestrator) so they are scored by the same rules.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep, RiskTier
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools import read_meta

if TYPE_CHECKING:
    from incident_agent.agents.llm_react_agent import LLMDiagnosis


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_.\-]{3,}", text.lower()))


def is_grounded(detail: str, seen: set[str], threshold: float = 0.5) -> bool:
    """True when at least ``threshold`` of the claim's tokens occur in the tool output (``seen``)."""
    toks = tokens(detail)
    return bool(toks) and len(toks & seen) / len(toks) >= threshold


def ground_diagnosis(
    agent_type: str,
    incident_id: str,
    out: LLMDiagnosis,
    transcript: str,
    *,
    confidence_cap: float | None = None,
    notes: list[str] | None = None,
) -> Diagnosis:
    """Validate the answer against what the tools really returned; ungrounded evidence costs confidence."""
    seen = tokens(transcript)
    evidence: list[EvidenceItem] = []
    ungrounded = 0
    for e in out.evidence:
        grounded = is_grounded(e.detail, seen)
        ungrounded += not grounded
        evidence.append(
            EvidenceItem(
                source_type=e.source_type,
                source=e.source,
                detail=e.detail,
                timestamp=e.timestamp,
                confidence_weight=1.0 if grounded else 0.3,
            )
        )
    steps = [ReasoningStep(step_number=i, description=s) for i, s in enumerate(out.reasoning_steps, 1)]
    for note in notes or []:
        steps.append(ReasoningStep(step_number=len(steps) + 1, description=note))
    confidence = out.confidence
    if ungrounded:
        confidence = round(max(0.0, confidence - 0.15 * ungrounded), 2)
        steps.append(
            ReasoningStep(
                step_number=len(steps) + 1,
                description=f"Grounding check: {ungrounded} evidence item(s) not found in tool output; "
                "confidence reduced.",
            )
        )
    if confidence_cap is not None:
        confidence = min(confidence, confidence_cap)
    root = out.root_cause
    if out.is_false_alarm and "false alarm" not in root.lower():
        root = f"False alarm: no real incident. {root}"
    return Diagnosis(
        agent_type=agent_type,
        incident_id=incident_id,
        root_cause=root,
        confidence=confidence,
        evidence=evidence,
        affected_service=out.affected_service,
        category=out.category,
        reasoning_steps=steps,
        recommendation=out.recommendation,
        risk_tier=out.risk_tier,
    )


def degraded_diagnosis(agent_type: str, incident_id: str, transcript: str, reason: str, calls: int) -> Diagnosis:
    """Valid, clearly-flagged, zero-confidence result so one outage never loses the whole incident."""
    try:
        service = str(read_meta(incident_id)["services"][0])
    except (OSError, KeyError, IndexError, ValueError):
        service = "unknown"
    return Diagnosis(
        agent_type=agent_type,
        incident_id=incident_id,
        root_cause=f"Unknown (degraded run): no reliable root cause; {calls} tool call(s) completed.",
        confidence=0.0,
        evidence=[
            EvidenceItem(
                source_type=EvidenceType.META,
                source="agent_transcript",
                detail=transcript[:300] or "no tool output collected",
                confidence_weight=0.0,
            )
        ],
        affected_service=service,
        category="undetermined",
        reasoning_steps=[ReasoningStep(step_number=1, description=f"Degraded: {reason}")],
        recommendation="Escalate to a human on-call engineer; automated diagnosis did not complete.",
        risk_tier=RiskTier.HIGH,
    )
