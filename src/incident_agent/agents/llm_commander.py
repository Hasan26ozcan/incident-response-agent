"""LLM Incident Commander (Stage 7): reviews the orchestrator's work and owns the escalation decision.

The commander has no tools. It receives the orchestrator's ``Diagnosis`` and the worker reports and:

* writes the incident narrative (an LLM call; time stamps in it are checked against tool output),
* confirms or *revises* the diagnosis. A revision is accepted only if its supporting quotes are found in
  the workers' tool output; otherwise the orchestrator's diagnosis stands and the rejection is recorded,
* assigns a severity (SEV1-SEV4) with a rationale,
* decides escalation with a deterministic, auditable policy (``decide_escalation``): the model never
  decides whether a human is paged.

If the LLM fails the commander falls back to a template narrative and a risk-tier severity; the
diagnosis is never lost. No remediation is executed here (roadmap: "no actions yet").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from incident_agent.agents.grounding import is_grounded, tokens
from incident_agent.agents.llm_orchestrator import WORKER_ORDER, WorkerOutcome, reports_json
from incident_agent.agents.risk_guard import apply_risk_guard
from incident_agent.llm import LLMClient, LLMUnavailable, StructuredOutputError, generate_structured
from incident_agent.prompts import commander_prompts as P
from incident_agent.schemas.agent_output import (
    EvidenceItem,
    EvidenceType,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.schemas.command_diagnosis import CommanderDiagnosis
from incident_agent.schemas.diagnosis import Diagnosis

Severity = Literal["SEV1", "SEV2", "SEV3", "SEV4"]
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_EVIDENCE_TYPE = {
    "log": EvidenceType.LOG_ENTRY,
    "metrics": EvidenceType.METRIC_ANOMALY,
    "deploy": EvidenceType.DEPLOY,
}
ONSET_AGREEMENT_MINUTES = 10


class RevisedDiagnosis(BaseModel):
    is_false_alarm: bool
    root_cause: str = Field(min_length=10)
    category: str = Field(min_length=1)
    recommendation: str = Field(min_length=10)
    risk_tier: RiskTier
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_quotes: list[str] = Field(min_length=1, max_length=5)


class CommanderReview(BaseModel):
    narrative: str = Field(min_length=40, max_length=1500)
    agrees_with_diagnosis: bool
    disagreement: str = ""
    revised: RevisedDiagnosis | None = None
    severity: Severity
    severity_rationale: str = Field(min_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=4)


@dataclass
class Signals:
    """Coverage/consistency facts computed by code."""

    workers_total: int
    workers_ok: int
    failed: list[str]
    normal_domains: list[str]
    anomalous_domains: list[str]
    verified_ratio: float  # verified facts / all facts (1.0 when there are no facts)
    onset_spread_minutes: int | None  # None when fewer than two parseable onsets
    all_domains_normal: bool = False

    @property
    def onsets_agree(self) -> bool:
        return self.onset_spread_minutes is None or self.onset_spread_minutes <= ONSET_AGREEMENT_MINUTES

    def describe(self) -> str:
        spread = "n/a" if self.onset_spread_minutes is None else f"{self.onset_spread_minutes} min"
        return (
            f"workers_ok={self.workers_ok}/{self.workers_total}; failed={self.failed or 'none'}; "
            f"anomalous={self.anomalous_domains or 'none'}; normal={self.normal_domains or 'none'}; "
            f"onset_spread={spread}; verified_fact_ratio={self.verified_ratio:.2f}"
        )


def _minutes(text: str) -> int | None:
    m = _TIME.search(text)
    if not m:
        return None
    hh, mm = m.group(0).split(":")
    return int(hh) * 60 + int(mm)


def compute_signals(outcomes: list[WorkerOutcome]) -> Signals:
    ok = [o for o in outcomes if o.report is not None]
    facts = verified = 0
    onsets: list[int] = []
    for o in outcomes:
        rep = o.report
        if rep is None:
            continue
        seen = tokens("\n".join(o.transcript))
        for f in rep.facts:
            facts += 1
            verified += is_grounded(f.quote, seen)
        if not rep.domain_normal and (t := _minutes(rep.anomaly_onset)) is not None:
            onsets.append(t)
    normal = [o.name for o in ok if o.report and o.report.domain_normal]
    anomalous = [o.name for o in ok if o.report and not o.report.domain_normal]
    return Signals(
        workers_total=len(outcomes),
        workers_ok=len(ok),
        failed=[o.name for o in outcomes if o.report is None],
        normal_domains=normal,
        anomalous_domains=anomalous,
        verified_ratio=(verified / facts) if facts else 1.0,
        onset_spread_minutes=(max(onsets) - min(onsets)) if len(onsets) >= 2 else None,
        all_domains_normal=bool(ok) and not anomalous,
    )


def claims_false_alarm(root_cause: str) -> bool:
    return root_cause.lower().lstrip().startswith("false alarm")


def decide_escalation(
    *,
    risk_tier: RiskTier,
    confidence: float,
    signals: Signals,
    degraded: bool,
    false_alarm: bool,
    threshold: float = 0.7,
) -> tuple[str, str]:
    """Deterministic, auditable escalation policy. Returns (decision, reason). First matching rule wins."""
    if degraded:
        return "escalate", "automated diagnosis was degraded; a human must take over"
    if risk_tier is RiskTier.HIGH:
        return "escalate", "high-risk remediation requires human approval"
    if confidence < threshold:
        return "escalate", f"confidence {confidence:.0%} is below the {threshold:.0%} threshold"
    if false_alarm:
        confirmed = signals.all_domains_normal and signals.workers_ok == signals.workers_total
        if confirmed and risk_tier is RiskTier.LOW:
            return "resolve", "false alarm confirmed: every domain looks normal"
        return "monitor", "false-alarm claim is not confirmed by every domain, or still carries a remediation"
    if risk_tier is RiskTier.MEDIUM:
        return "monitor", "medium-risk remediation: watch while it is applied with approval"
    if signals.workers_ok < signals.workers_total:
        return "monitor", f"incomplete coverage ({signals.workers_ok}/{signals.workers_total} workers)"
    return "resolve", "low risk, confident, full coverage"


def fallback_severity(risk_tier: RiskTier, false_alarm: bool) -> tuple[Severity, str]:
    if false_alarm:
        return "SEV4", "false alarm; no real impact (fallback rule, commander model unavailable)"
    table: dict[RiskTier, Severity] = {RiskTier.HIGH: "SEV2", RiskTier.MEDIUM: "SEV3", RiskTier.LOW: "SEV4"}
    return table[
        risk_tier
    ], f"derived from {risk_tier.value}-risk remediation (fallback rule, commander model unavailable)"


def report_to_finding(outcome: WorkerOutcome) -> WorkerFinding | None:
    rep = outcome.report
    if rep is None:
        return None
    seen = tokens("\n".join(outcome.transcript))
    evidence = [
        EvidenceItem(
            source_type=_EVIDENCE_TYPE[outcome.name],
            source=f"{outcome.name}:worker_report",
            detail=f.quote,
            timestamp=f.timestamp,
            confidence_weight=1.0 if is_grounded(f.quote, seen) else 0.3,
        )
        for f in rep.facts
    ] or [
        EvidenceItem(
            source_type=EvidenceType.META,
            source=f"{outcome.name}:worker_report",
            detail=rep.summary,
            confidence_weight=0.5,
        )
    ]
    steps = [ReasoningStep(step_number=1, description=f"{outcome.name} worker: {rep.summary}")]
    if rep.suspected_cause:
        steps.append(ReasoningStep(step_number=2, description=f"Suspected cause: {rep.suspected_cause}"))
    return WorkerFinding(
        worker_type=outcome.name,
        incident_id="INC-000",  # replaced by the caller (needs the real id)
        evidence=evidence,
        reasoning_steps=steps,
        confidence=rep.confidence,
        summary=rep.summary,
    )


def _fallback_narrative(incident_id: str, diagnosis: Diagnosis, signals: Signals, outcomes: list[WorkerOutcome]) -> str:
    parts = [f"Incident {incident_id} affects {diagnosis.affected_service}. Diagnosis: {diagnosis.root_cause}"]
    for o in outcomes:
        if o.report:
            onset = f" (onset {o.report.anomaly_onset})" if o.report.anomaly_onset else ""
            parts.append(f"The {o.name} worker reports: {o.report.summary}{onset}")
    if signals.failed:
        parts.append(f"No report from: {', '.join(signals.failed)}.")
    return " ".join(parts)


@dataclass
class CommanderResult:
    output: CommanderDiagnosis
    review: CommanderReview | None
    escalation_reason: str
    revision_applied: bool = False
    notes: list[str] = field(default_factory=list)
    fallback_reason: str = ""


class LLMIncidentCommander:
    agent_type = "llm_swarm"

    def __init__(
        self,
        client: LLMClient,
        *,
        allow_revision: bool = True,
        confidence_threshold: float = 0.7,
        temperature: float = 0.0,
        max_repairs: int = 2,
        risk_guard: bool = False,
    ) -> None:
        self.client = client
        self.allow_revision = allow_revision
        self.confidence_threshold = confidence_threshold
        self.temperature = temperature
        self.max_repairs = max_repairs
        self.risk_guard = risk_guard

    # -- review ---------------------------------------------------------------
    def _review(self, incident_id: str, meta: str, diagnosis: Diagnosis, outcomes: list[WorkerOutcome], sig: Signals):
        diag = {
            "root_cause": diagnosis.root_cause,
            "category": diagnosis.category,
            "affected_service": diagnosis.affected_service,
            "confidence": diagnosis.confidence,
            "recommendation": diagnosis.recommendation,
            "risk_tier": diagnosis.risk_tier.value,
            "evidence": [e.detail for e in diagnosis.evidence][:6],
        }
        messages = [
            {"role": "system", "content": P.COMMANDER_SYSTEM},
            {
                "role": "user",
                "content": P.COMMANDER_USER.format(
                    incident_id=incident_id,
                    meta=meta,
                    diagnosis=json.dumps(diag, indent=1),
                    reports=reports_json(outcomes, limit=7000),
                    signals=sig.describe(),
                ),
            },
        ]
        return generate_structured(
            self.client, messages, CommanderReview, max_repairs=self.max_repairs, temperature=self.temperature
        )

    def _apply_revision(
        self, diagnosis: Diagnosis, review: CommanderReview, transcript: str, notes: list[str]
    ) -> tuple[Diagnosis, bool]:
        rev = review.revised
        if review.agrees_with_diagnosis or rev is None:
            return diagnosis, False
        if not self.allow_revision:
            notes.append(f"Commander disagreed but revisions are disabled: {review.disagreement or 'no reason given'}")
            return diagnosis, False
        seen = tokens(transcript)
        grounded = [is_grounded(q, seen) for q in rev.supporting_quotes]
        if sum(grounded) / len(grounded) < 0.5:
            notes.append("Commander revision rejected: its supporting quotes are not in the tool output.")
            return diagnosis, False
        root = rev.root_cause
        if rev.is_false_alarm and not claims_false_alarm(root):
            root = f"False alarm: no real incident. {root}"
        quotes = [
            EvidenceItem(
                source_type=EvidenceType.META,
                source="commander:quote",
                detail=q,
                confidence_weight=1.0 if ok else 0.3,
            )
            for q, ok in zip(rev.supporting_quotes, grounded, strict=True)
        ]
        confidence = round(max(0.0, rev.confidence - 0.15 * (len(grounded) - sum(grounded))), 2)
        steps = [
            *diagnosis.reasoning_steps,
            ReasoningStep(
                step_number=len(diagnosis.reasoning_steps) + 1,
                description=f"Commander revised the diagnosis: {review.disagreement or 'evidence contradicts it'}",
            ),
        ]
        revised = Diagnosis(
            agent_type=self.agent_type,
            incident_id=diagnosis.incident_id,
            root_cause=root,
            confidence=confidence,
            evidence=[*diagnosis.evidence, *quotes],
            affected_service=diagnosis.affected_service,
            category=rev.category,
            reasoning_steps=steps,
            recommendation=rev.recommendation,
            risk_tier=rev.risk_tier,
        )
        return revised, True

    # -- main ---------------------------------------------------------------
    def run(
        self,
        incident_id: str,
        meta: str,
        diagnosis: Diagnosis,
        outcomes: list[WorkerOutcome],
        *,
        degraded: bool = False,
    ) -> CommanderResult:
        sig = compute_signals(outcomes)
        transcript = "\n\n".join("\n\n".join(o.transcript) for o in outcomes)
        notes: list[str] = []
        review: CommanderReview | None = None
        fallback = ""
        if degraded:
            fallback = "orchestrator degraded: nothing to review"
        else:
            try:
                review = self._review(incident_id, meta, diagnosis, outcomes, sig)
            except (LLMUnavailable, StructuredOutputError) as exc:
                fallback = f"{type(exc).__name__}: {str(exc)[:160]}"

        final, revised = diagnosis, False
        if review is not None:
            final, revised = self._apply_revision(diagnosis, review, transcript, notes)
            if self.risk_guard and revised:
                final = apply_risk_guard(final)[0]
            narrative = review.narrative
            severity, rationale = review.severity, review.severity_rationale
            questions = review.open_questions
            base_conf = final.confidence
            stale = sorted(set(_TIME.findall(narrative)) - set(_TIME.findall(transcript)))
            if stale:
                notes.append(f"Narrative mentions time(s) not present in tool output: {', '.join(stale)}")
                base_conf = round(max(0.0, base_conf - min(0.15, 0.05 * len(stale))), 2)
        else:
            narrative = _fallback_narrative(incident_id, diagnosis, sig, outcomes)
            severity, rationale = fallback_severity(diagnosis.risk_tier, claims_false_alarm(diagnosis.root_cause))
            questions = []
            base_conf = diagnosis.confidence
            if not degraded:
                notes.append(f"Commander model unavailable ({fallback}); template narrative and rule-based severity.")

        confidence = round(max(0.0, base_conf - 0.1 * len(sig.failed)), 2)
        if sig.failed:
            notes.append(f"Coverage: {len(sig.failed)} worker(s) failed ({', '.join(sig.failed)}); confidence reduced.")
        if not sig.onsets_agree:
            notes.append(f"Workers disagree on onset time (spread {sig.onset_spread_minutes} min).")

        decision, reason = decide_escalation(
            risk_tier=final.risk_tier,
            confidence=confidence,
            signals=sig,
            degraded=degraded,
            false_alarm=claims_false_alarm(final.root_cause),
            threshold=self.confidence_threshold,
        )
        findings = []
        for o in outcomes:
            f = report_to_finding(o)
            if f is not None:
                findings.append(f.model_copy(update={"incident_id": incident_id}))
        if not findings:
            findings = [
                WorkerFinding(
                    worker_type="none",
                    incident_id=incident_id,
                    evidence=[
                        EvidenceItem(
                            source_type=EvidenceType.META,
                            source="commander:no_worker_data",
                            detail="No worker produced a report for this incident",
                            confidence_weight=0.0,
                        )
                    ],
                    reasoning_steps=[ReasoningStep(step_number=1, description="All workers failed")],
                    confidence=0.0,
                    summary="No specialist worker produced a report for this incident",
                )
            ]
        evidence = list(final.evidence)
        seen_detail = {e.detail for e in evidence}
        for f in findings:
            for e in f.evidence:
                if e.detail not in seen_detail and e.confidence_weight >= 1.0:
                    evidence.append(e)
                    seen_detail.add(e.detail)
        steps = [
            ReasoningStep(
                step_number=1,
                description=f"Reviewed the {diagnosis.agent_type} diagnosis against {sig.workers_ok}/"
                f"{sig.workers_total} worker reports ({sig.describe()})",
            ),
            ReasoningStep(
                step_number=2,
                description="Commander "
                + ("revised the diagnosis." if revised else "kept the orchestrator's diagnosis.")
                + (f" Reason: {review.disagreement}" if review and review.disagreement else ""),
            ),
            ReasoningStep(step_number=3, description=f"Escalation rule: {decision} - {reason}"),
            ReasoningStep(step_number=4, description=f"Severity {severity}: {rationale}"),
        ]
        steps += [ReasoningStep(step_number=len(steps) + i, description=n) for i, n in enumerate(notes, 1)]
        verb = {"escalate": "ESCALATE to the on-call engineer", "monitor": "MONITOR", "resolve": "RESOLVE"}[decision]
        recommendation = f"{verb} ({reason}). Proposed remediation, not executed: {final.recommendation}"
        if questions:
            recommendation += " Open questions: " + "; ".join(questions)
        out = CommanderDiagnosis(
            agent_type=self.agent_type,
            incident_id=incident_id,
            narrative=narrative,
            escalation_decision=decision,
            severity_assessment=f"{severity} - {rationale}",
            confidence=confidence,
            risk_tier=final.risk_tier,
            diagnosis=final,
            worker_findings=findings,
            evidence=evidence,
            affected_service=final.affected_service,
            category=final.category,
            recommendation=recommendation,
            reasoning_steps=steps,
        )
        return CommanderResult(out, review, reason, revised, notes, fallback)


__all__ = [
    "WORKER_ORDER",
    "CommanderResult",
    "CommanderReview",
    "LLMIncidentCommander",
    "RevisedDiagnosis",
    "Signals",
    "compute_signals",
    "decide_escalation",
    "fallback_severity",
]
