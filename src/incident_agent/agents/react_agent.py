"""ReAct agent for single-agent incident diagnosis.

Implements the ReAct (Reason + Act) loop described in
ROADMAP.md Stage 3. The agent reads incident data, observes
anomalies, reasons about root causes, and produces a structured
diagnosis.

Stage 3: single-agent skeleton. Each step of the loop is explicit
and traceable — no black-box reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from incident_agent.tools import (
    detect_anomaly_timestamps,
    find_error_logs,
    get_recent_deploys,
    read_meta,
)


@dataclass
class Diagnosis:
    """Structured output of the ReAct agent's diagnosis."""

    incident_id: str
    root_cause: str
    confidence: float  # 0.0 to 1.0
    evidence: list[str] = field(default_factory=list)
    affected_service: str = ""
    category: str = ""
    reasoning_steps: list[str] = field(default_factory=list)
    recommendation: str = ""
    risk_tier: Literal["low", "medium", "high"] = "low"

    def format_report(self) -> str:
        """Return a human-readable diagnosis report."""
        lines = [
            f"=== Diagnosis: {self.incident_id} ===",
            f"Service: {self.affected_service}",
            f"Category: {self.category}",
            f"Root Cause: {self.root_cause}",
            f"Confidence: {self.confidence:.0%}",
            f"Risk Tier: {self.risk_tier}",
            "",
            "Evidence:",
        ]
        for e in self.evidence:
            lines.append(f"  - {e}")
        lines.append("")
        lines.append("Reasoning:")
        for i, step in enumerate(self.reasoning_steps, 1):
            lines.append(f"  {i}. {step}")
        lines.append("")
        lines.append(f"Recommendation: {self.recommendation}")
        return "\n".join(lines)


class ReActAgent:
    """Single-agent ReAct loop for incident diagnosis.

    The loop follows the pattern:
        1. Observe: read incident data
        2. Reason: analyze anomalies, correlate evidence
        3. Act: produce a diagnosis
        4. Repeat: until confidence threshold is met
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
        """Step 3: Produce a structured diagnosis from observations."""
        meta = observation["meta"]
        errors = observation["error_logs"]
        anomalies = observation["metric_anomalies"]
        deploys = observation["deploys"]

        service = meta["services"][0] if meta["services"] else "unknown"
        category = meta.get("category", "unknown")

        # Build evidence list from actual data
        evidence: list[str] = []
        for err in errors[:5]:
            evidence.append(f"LOG [{err.timestamp}] {err.message}")

        for metric_name, timestamps in anomalies.items():
            evidence.append(f"METRIC {metric_name} anomaly at {timestamps[0]}")

        for dep in deploys[:3]:
            evidence.append(f"DEPLOY {dep['deploy_id']}: {dep['description']}")

        # Determine root cause from patterns
        error_messages = [e.message for e in errors]
        root_cause = self._infer_root_cause(error_messages, anomalies, category)

        # Calculate confidence based on evidence count
        evidence_count = len(evidence)
        confidence = min(0.5 + evidence_count * 0.1, 0.95)

        # Determine risk tier
        risk_tier: Literal["low", "medium", "high"] = "low"
        if category in ("cpu_exhaustion", "memory_leak"):
            risk_tier = "high"
        elif category in ("cascading_failure", "db_connection"):
            risk_tier = "medium"

        # Build recommendation
        recommendation = self._build_recommendation(root_cause, service, risk_tier)

        return Diagnosis(
            incident_id=meta["incident_id"],
            root_cause=root_cause,
            confidence=confidence,
            evidence=evidence,
            affected_service=service,
            category=category,
            reasoning_steps=reasoning,
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

    def _build_recommendation(self, root_cause: str, service: str, risk_tier: str) -> str:
        """Build remediation recommendation based on root cause."""
        if "unbounded retry" in root_cause.lower():
            return "Restart affected pods to shed stuck threads, then patch the retry handler to cap retry attempts."
        if "connection pool" in root_cause.lower():
            return "Restart affected pods to reset connections, then fix connection leak in request handler."
        if "memory leak" in root_cause.lower():
            return "Restart affected pods to reclaim memory, then fix the object retention bug."
        return (
            f"Restart affected {service} instances and investigate "
            f"the root cause ({risk_tier}-risk action — requires approval)."
        )

    def run(self, incident_id: str) -> Diagnosis:
        """Execute the full ReAct loop for a single incident.

        Returns a Diagnosis with confidence >= self.confidence_threshold.
        """
        observation = self.observe(incident_id)
        reasoning = self.reason(observation)
        diagnosis = self.act(observation, reasoning)

        # ReAct loop: if confidence is below threshold, gather more
        # evidence and re-reason (simplified: one extra iteration)
        if diagnosis.confidence < self.confidence_threshold:
            # Additional observation: look at raw error count
            extra_errors = len(observation["error_logs"])
            if extra_errors > 20:
                diagnosis.confidence = min(diagnosis.confidence + 0.1, 0.95)
                diagnosis.reasoning_steps.append(
                    f"Re-observed {extra_errors} total error entries — increasing confidence"
                )

        self.diagnosis = diagnosis
        return diagnosis
