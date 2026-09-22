"""Incident Commander for hierarchical swarm architecture.

Stage 7 introduces the Incident Commander agent, which sits
above the OrchestratorAgent in the hierarchy. The Commander
receives the orchestrator's Diagnosis and all specialist
worker findings, then synthesizes them into a unified
incident narrative with an escalation decision.

The Commander does not perform remediation actions — it
provides the strategic overview and escalation decisions
that guide human responders.

See ROADMAP.md Phase C — Stage 7 for the full spec.
"""

from __future__ import annotations

from incident_agent.schemas.agent_output import (
    EvidenceItem,
    EvidenceType,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.schemas.command_diagnosis import CommanderDiagnosis
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools import read_meta


class IncidentCommander:
    """Incident Commander agent for hierarchical swarm diagnosis.

    The Commander sits above the OrchestratorAgent in the
    multi-agent hierarchy. It receives the orchestrator's
    Diagnosis and all specialist worker findings, then
    produces a CommanderDiagnosis containing:
      - A unified incident narrative
      - An escalation decision (escalate / monitor / resolve)
      - A severity assessment
      - Commander-level reasoning and recommendations

    Responsibility (per OpenSpec): synthesize worker findings
    into a unified incident narrative, make escalation
    decisions, assess overall severity.

    All outputs are validated Pydantic ``CommanderDiagnosis``
    objects (Stage 4 cross-cutting rule).
    """

    def __init__(
        self,
        confidence_threshold: float = 0.7,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.commander_diagnosis: CommanderDiagnosis | None = None

    def synthesize_narrative(
        self,
        diagnosis: Diagnosis,
        worker_findings: dict[str, WorkerFinding],
        incident_id: str,
    ) -> str:
        """Build a unified incident narrative from all findings.

        Combines the orchestrator's root-cause diagnosis with
        the evidence and summaries from each specialist worker
        into a coherent story of what happened.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            worker_findings: Dict mapping worker_type to WorkerFinding.
            incident_id: The incident identifier.

        Returns:
            A unified incident narrative string.
        """
        meta = read_meta(incident_id)
        category = meta.get("category", "unknown")
        services = meta.get("services", [])
        service = services[0] if services else "unknown"

        # Build narrative from diagnosis root cause and worker summaries
        worker_summaries = []
        for worker_type, finding in worker_findings.items():
            worker_summaries.append(f"The {worker_type} analysis found: {finding.summary}")

        worker_section = (
            " ".join(worker_summaries)
            if worker_summaries
            else "No specialist worker findings available for this incident."
        )

        risk_label = (
            "high"
            if diagnosis.risk_tier == RiskTier.HIGH
            else "moderate"
            if diagnosis.risk_tier == RiskTier.MEDIUM
            else "standard"
        )
        response_label = (
            "immediate escalation"
            if diagnosis.risk_tier == RiskTier.HIGH
            else "continued monitoring"
            if diagnosis.risk_tier == RiskTier.MEDIUM
            else "standard handling"
        )

        narrative = (
            f"Incident {incident_id} affects the {service} service "
            f"and is classified as {category}. "
            f"The orchestrator diagnosed the root cause as: "
            f"{diagnosis.root_cause}. "
            f"Confidence in this diagnosis is {diagnosis.confidence:.0%}. "
            f"The risk tier is {diagnosis.risk_tier.value}. "
            f"Specialist analysis summary: {worker_section} "
            f"The incident requires a {risk_label} level of response "
            f"and {response_label}."
        )

        return narrative

    def determine_escalation(
        self,
        diagnosis: Diagnosis,
        worker_findings: dict[str, WorkerFinding],
    ) -> str:
        """Determine the escalation decision.

        Decisions are based on the risk tier, confidence,
        and the number of affected workers.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            worker_findings: Dict mapping worker_type to WorkerFinding.

        Returns:
            Escalation decision: "escalate", "monitor", or "resolve".
        """
        risk_tier = diagnosis.risk_tier
        confidence = diagnosis.confidence
        worker_count = len(worker_findings)

        # High-risk incidents always escalate
        if risk_tier == RiskTier.HIGH:
            return "escalate"

        # Medium-risk with low confidence escalates
        if risk_tier == RiskTier.MEDIUM and confidence < self.confidence_threshold:
            return "escalate"

        # Medium-risk with good confidence is monitored
        if risk_tier == RiskTier.MEDIUM:
            return "monitor"

        # Low-risk with multiple worker findings suggests monitoring
        if risk_tier == RiskTier.LOW and worker_count >= 3:
            return "monitor"

        # Low-risk with good confidence can be resolved
        if risk_tier == RiskTier.LOW and confidence >= self.confidence_threshold:
            return "resolve"

        return "monitor"

    def assess_severity(
        self,
        diagnosis: Diagnosis,
        worker_findings: dict[str, WorkerFinding],
    ) -> str:
        """Assess the overall severity of the incident.

        Considers risk tier, confidence, evidence count,
        and number of affected workers.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            worker_findings: Dict mapping worker_type to WorkerFinding.

        Returns:
            A severity assessment string.
        """
        risk_tier = diagnosis.risk_tier
        evidence_count = len(diagnosis.evidence)
        worker_count = len(worker_findings)

        if risk_tier == RiskTier.HIGH:
            if evidence_count > 10:
                return "Critical — multiple evidence sources confirm high-severity incident"
            return "Critical — high-risk incident confirmed"

        if risk_tier == RiskTier.MEDIUM:
            if worker_count >= 2:
                return "Elevated — multiple domains affected, moderate risk"
            return "Elevated — medium-risk incident with sufficient evidence"

        # Low risk
        if evidence_count > 5:
            return "Low — minor incident with substantial evidence"
        return "Low — routine incident with limited impact"

    def build_reasoning(
        self,
        diagnosis: Diagnosis,
        worker_findings: dict[str, WorkerFinding],
        narrative: str,
    ) -> list[ReasoningStep]:
        """Build commander-level reasoning steps.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            worker_findings: Dict mapping worker_type to WorkerFinding.
            narrative: The unified incident narrative.

        Returns:
            A list of ReasoningStep objects.
        """
        steps: list[ReasoningStep] = []

        # Step 1: Commander review
        steps.append(
            ReasoningStep(
                step_number=1,
                description=f"Incident Commander reviewed orchestrator diagnosis for {diagnosis.incident_id}",
                evidence_refs=[],
            )
        )

        # Step 2: Worker findings synthesis
        worker_types = list(worker_findings.keys())
        worker_types_str = ", ".join(worker_types)
        steps.append(
            ReasoningStep(
                step_number=2,
                description=(f"Synthesized findings from {len(worker_types)} specialist workers: {worker_types_str}"),
                evidence_refs=[],
            )
        )

        # Step 3: Narrative assessment
        steps.append(
            ReasoningStep(
                step_number=3,
                description=f"Narrative assessment: {narrative[:80]}...",
                evidence_refs=[],
            )
        )

        # Step 4: Escalation reasoning
        risk_tier = diagnosis.risk_tier.value
        steps.append(
            ReasoningStep(
                step_number=4,
                description=(
                    f"Escalation decision based on {risk_tier}-risk tier and {diagnosis.confidence:.0%} confidence"
                ),
                evidence_refs=[],
            )
        )

        return steps

    def build_evidence(
        self,
        diagnosis: Diagnosis,
        worker_findings: dict[str, WorkerFinding],
    ) -> list[EvidenceItem]:
        """Aggregate evidence from the diagnosis and all workers.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            worker_findings: Dict mapping worker_type to WorkerFinding.

        Returns:
            A combined list of EvidenceItem objects.
        """
        all_evidence: list[EvidenceItem] = list(diagnosis.evidence)

        # Add any additional evidence from workers not already in diagnosis
        for worker_type, finding in worker_findings.items():
            for ev in finding.evidence:
                # Avoid duplicates by checking source
                if not any(e.source == ev.source for e in all_evidence):
                    all_evidence.append(ev)

        return all_evidence

    def build_recommendation(
        self,
        diagnosis: Diagnosis,
        escalation: str,
    ) -> str:
        """Build a commander-level recommendation.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            escalation: The escalation decision.

        Returns:
            A commander-level recommendation string.
        """
        if escalation == "escalate":
            return (
                f"IMMEDIATE ESCALATION: {diagnosis.incident_id} requires "
                f"immediate human attention. {diagnosis.root_cause}. "
                f"All available resources should be mobilized. "
                f"Follow the incident response playbook for {diagnosis.category}."
            )
        if escalation == "monitor":
            return (
                f"CONTINUED MONITORING: {diagnosis.incident_id} should be "
                f"monitored closely. {diagnosis.root_cause}. "
                f"Check back in 15 minutes for status update. "
                f"If symptoms worsen, escalate immediately."
            )
        return (
            f"RESOLUTION PATH: {diagnosis.incident_id} can be resolved "
            f"through automated remediation. {diagnosis.root_cause}. "
            f"Verify resolution after remediation steps complete."
        )

    def run(
        self,
        diagnosis: Diagnosis,
        worker_findings: dict[str, WorkerFinding],
        incident_id: str,
    ) -> CommanderDiagnosis:
        """Execute the Incident Commander pipeline.

        Pipeline: synthesize narrative → determine escalation →
        assess severity → build reasoning → produce CommanderDiagnosis.

        Args:
            diagnosis: The orchestrator's Diagnosis.
            worker_findings: Dict mapping worker_type to WorkerFinding.
            incident_id: The incident identifier.

        Returns:
            A validated ``CommanderDiagnosis`` object.
        """
        # Step 1: Synthesize the incident narrative
        narrative = self.synthesize_narrative(diagnosis, worker_findings, incident_id)

        # Step 2: Determine escalation decision
        escalation = self.determine_escalation(diagnosis, worker_findings)

        # Step 3: Assess overall severity
        severity = self.assess_severity(diagnosis, worker_findings)

        # Step 4: Build commander-level evidence
        evidence = self.build_evidence(diagnosis, worker_findings)

        # Step 5: Build commander-level reasoning
        reasoning_steps = self.build_reasoning(diagnosis, worker_findings, narrative)

        # Step 6: Build recommendation
        recommendation = self.build_recommendation(diagnosis, escalation)

        # Step 7: Calculate commander confidence
        commander_confidence = min(diagnosis.confidence + 0.05, 0.95)

        # Step 8: Build worker findings list
        # Ensure at least one finding exists (CommanderDiagnosis requires min 1)
        worker_list = list(worker_findings.values())
        if not worker_list:
            worker_list = [
                WorkerFinding(
                    worker_type="fallback",
                    incident_id=incident_id,
                    evidence=[
                        EvidenceItem(
                            source_type=EvidenceType.META,
                            source="cmdr:fallback",
                            detail="Fallback evidence — no specialist worker data available",
                            confidence_weight=0.0,
                        )
                    ],
                    reasoning_steps=[
                        ReasoningStep(
                            step_number=1,
                            description="No worker findings available",
                            evidence_refs=[],
                        )
                    ],
                    confidence=0.0,
                    summary="No specialist worker findings available for this incident",
                )
            ]

        # Step 9: Create the CommanderDiagnosis
        commander_diagnosis = CommanderDiagnosis(
            agent_type="incident_commander",
            incident_id=incident_id,
            narrative=narrative,
            escalation_decision=escalation,
            severity_assessment=severity,
            confidence=commander_confidence,
            risk_tier=diagnosis.risk_tier,
            diagnosis=diagnosis,
            worker_findings=worker_list,
            evidence=evidence,
            affected_service=diagnosis.affected_service,
            category=diagnosis.category,
            recommendation=recommendation,
            reasoning_steps=reasoning_steps,
        )

        self.commander_diagnosis = commander_diagnosis
        return commander_diagnosis
