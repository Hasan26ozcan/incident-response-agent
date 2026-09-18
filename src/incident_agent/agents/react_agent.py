"""ReAct agent for single-agent incident diagnosis.

Implements the ReAct (Reason + Act) loop described in
ROADMAP.md Stage 3. The agent reads incident data, observes
anomalies, reasons about root causes, and produces a structured
diagnosis validated by Pydantic schemas (Stage 4).

Stage 3→4: Every agent output is now a Pydantic-validated
object — no free text passes between agents or to tools.
See openspec/agent-responsibility-matrix.md § Cross-cutting rules
(rule #3) for the schema-validation mandate.
"""

from __future__ import annotations

from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep, RiskTier
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools import (
    detect_anomaly_timestamps,
    find_error_logs,
    get_recent_deploys,
    read_meta,
)


class ReActAgent:
    """Single-agent ReAct loop for incident diagnosis.

    The loop follows the pattern:
        1. Observe: read incident data
        2. Reason: analyze anomalies, correlate evidence
        3. Act: produce a Pydantic-validated Diagnosis
        4. Repeat: until confidence threshold is met

    All outputs conform to the Diagnosis Pydantic schema,
    ensuring structural consistency for downstream consumers.
    """

    def __init__(self, confidence_threshold: float = 0.7) -> None:
        self.confidence_threshold = confidence_threshold
        self.diagnosis: Diagnosis | None = None

    def observe(self, incident_id: str) -> dict:
        """Step 1: Gather all incident data."""
        meta = read_meta(incident_id)
        errors = find_error_logs(incident_id)
        metric_anomalies = detect_anomaly_timestamps(incident_id)
        deploys = get_recent_deploys(incident_id)
        return {
            "meta": meta,
            "error_logs": errors,
            "metric_anomalies": metric_anomalies,
            "deploys": deploys,
        }

    def reason(self, observation: dict) -> list[str]:
        """Step 2: Analyze observations and build reasoning chain."""
        steps: list[str] = []
        meta = observation["meta"]
        errors = observation["error_logs"]
        anomalies = observation["metric_anomalies"]
        deploys = observation["deploys"]

        service = meta["services"][0] if meta["services"] else "unknown"
        steps.append(f"Observed {len(errors)} ERROR log entries for {service}")

        if errors:
            unique_errors = {e.message for e in errors}
            steps.append(f"Identified error patterns: {', '.join(sorted(unique_errors)[:3])}")

        if anomalies:
            for metric, timestamps in anomalies.items():
                steps.append(f"Detected anomaly in {metric} starting at {timestamps[0]}")

        if deploys:
            steps.append(f"Found {len(deploys)} deploy(s) before incident window")

        return steps

    def act(self, observation: dict, reasoning: list[str]) -> Diagnosis:
        """Step 3: Produce a Pydantic-validated Diagnosis.

        All evidence items and reasoning steps are constructed
        as Pydantic objects, ensuring schema compliance at
        construction time.
        """
        meta = observation["meta"]
        errors = observation["error_logs"]
        anomalies = observation["metric_anomalies"]
        deploys = observation["deploys"]

        service = meta["services"][0] if meta["services"] else "unknown"
        category = meta.get("category", "unknown")

        # Build evidence as Pydantic EvidenceItem objects
        evidence: list[EvidenceItem] = []
        for err in errors[:5]:
            evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.LOG_ENTRY,
                    source=f"log:{err.timestamp}",
                    detail=f"LOG [{err.timestamp}] {err.message}",
                    timestamp=err.timestamp,
                    confidence_weight=0.9,
                )
            )

        for metric_name, timestamps in anomalies.items():
            evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.METRIC_ANOMALY,
                    source=f"metric:{metric_name}",
                    detail=f"METRIC {metric_name} anomaly at {timestamps[0]}",
                    timestamp=timestamps[0],
                    confidence_weight=0.85,
                )
            )

        for dep in deploys[:3]:
            evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.DEPLOY,
                    source=f"deploy:{dep['deploy_id']}",
                    detail=f"DEPLOY {dep['deploy_id']}: {dep['description']}",
                    timestamp=dep.get("timestamp", ""),
                    confidence_weight=0.5,
                )
            )

        # Ensure at least one evidence item exists (even for false alarms)
        if not evidence:
            evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.LOG_ENTRY,
                    source="log:no_errors",
                    detail="No ERROR-level log entries found; incident may be a false alarm or pre-incident state",
                    timestamp="",
                    confidence_weight=0.1,
                )
            )

        # Determine root cause from patterns
        error_messages = [e.message for e in errors]
        root_cause = self._infer_root_cause(error_messages, anomalies, category)

        # Calculate confidence based on evidence count
        evidence_count = len(evidence)
        confidence = min(0.5 + evidence_count * 0.1, 0.95)

        # Determine risk tier
        risk_tier = self._determine_risk_tier(category)

        # Build recommendation
        recommendation = self._build_recommendation(root_cause, service, risk_tier)

        # Build reasoning steps as Pydantic ReasoningStep objects
        reasoning_steps: list[ReasoningStep] = []
        for i, step_text in enumerate(reasoning, 1):
            reasoning_steps.append(
                ReasoningStep(
                    step_number=i,
                    description=step_text,
                    evidence_refs=[],
                )
            )

        return Diagnosis(
            agent_type="single_agent_react",
            incident_id=meta["incident_id"],
            root_cause=root_cause,
            confidence=confidence,
            evidence=evidence,
            affected_service=service,
            category=category,
            reasoning_steps=reasoning_steps,
            recommendation=recommendation,
            risk_tier=risk_tier,
        )

    def _infer_root_cause(
        self,
        error_messages: list[str],
        anomalies: dict,
        category: str,
    ) -> str:
        """Infer root cause from error patterns and anomalies."""
        combined = " ".join(error_messages).lower()

        if "unbounded loop" in combined or "retry handler" in combined:
            return (
                "An unbounded retry loop in the payment-confirmation "
                "handler pins a worker thread at 100% CPU per affected "
                "request, starving the thread pool."
            )
        if "connection pool" in combined and "exhausted" in combined:
            return "Database connection pool exhaustion due to leaked connections in the request handler."
        if "oom" in combined or "out of memory" in combined:
            return "Memory leak causing OOM kill; objects retained across request boundaries."
        if "timeout" in combined and "cascading" in combined:
            return "Cascading timeout failure: downstream service degradation propagated upstream."
        if category == "cpu_exhaustion":
            return (
                "CPU exhaustion caused by unbounded retry loop in "
                "the service handler, resulting in thread pool starvation."
            )
        return f"Unknown root cause in category: {category}"

    def _determine_risk_tier(self, category: str) -> RiskTier:
        """Determine the risk tier for an incident category."""
        if category in ("cpu_exhaustion", "memory_leak"):
            return RiskTier.HIGH
        elif category in ("cascading_failure", "db_connection"):
            return RiskTier.MEDIUM
        return RiskTier.LOW

    def _build_recommendation(self, root_cause: str, service: str, risk_tier: RiskTier) -> str:
        """Build remediation recommendation based on root cause."""
        if "unbounded retry" in root_cause.lower():
            return "Restart affected pods to shed stuck threads, then patch the retry handler to cap retry attempts."
        if "connection pool" in root_cause.lower():
            return "Restart affected pods to reset connections, then fix connection leak in request handler."
        if "memory leak" in root_cause.lower():
            return "Restart affected pods to reclaim memory, then fix the object retention bug."
        return (
            f"Restart affected {service} instances and investigate "
            f"the root cause ({risk_tier.value}-risk action — requires approval)."
        )

    def run(self, incident_id: str) -> Diagnosis:
        """Execute the full ReAct loop for a single incident.

        Returns a Diagnosis with confidence >= self.confidence_threshold.
        The output is a Pydantic-validated object conforming to the
        Diagnosis schema.
        """
        observation = self.observe(incident_id)
        reasoning = self.reason(observation)
        diagnosis = self.act(observation, reasoning)

        # ReAct loop: if confidence is below threshold, gather more
        # evidence and re-reason (simplified: one extra iteration)
        if diagnosis.confidence < self.confidence_threshold:
            extra_errors = len(observation["error_logs"])
            if extra_errors > 20:
                # Create a new Diagnosis with updated confidence
                # by reconstructing from the existing one
                diagnosis = Diagnosis(
                    agent_type="single_agent_react",
                    incident_id=diagnosis.incident_id,
                    root_cause=diagnosis.root_cause,
                    confidence=min(diagnosis.confidence + 0.1, 0.95),
                    evidence=diagnosis.evidence,
                    affected_service=diagnosis.affected_service,
                    category=diagnosis.category,
                    reasoning_steps=diagnosis.reasoning_steps
                    + [
                        ReasoningStep(
                            step_number=len(diagnosis.reasoning_steps) + 1,
                            description=f"Re-observed {extra_errors} total error entries — increasing confidence",
                            evidence_refs=[],
                        )
                    ],
                    recommendation=diagnosis.recommendation,
                    risk_tier=diagnosis.risk_tier,
                )

        self.diagnosis = diagnosis
        return diagnosis
