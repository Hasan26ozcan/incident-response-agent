"""Specialist worker agents for multi-agent incident diagnosis.

Stage 6 introduces the orchestrator–worker architecture:
a triage agent dispatches specialist workers to process
independent data domains (logs, metrics, deploy history).

Each worker uses the existing tool functions to read data
and produces a validated ``WorkerFinding`` Pydantic object.

See ROADMAP.md Phase C — Stage 6 for the full spec.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from incident_agent.schemas.agent_output import (
    EvidenceItem,
    EvidenceType,
    ReasoningStep,
    WorkerFinding,
)
from incident_agent.tools import (
    find_error_logs,
    get_recent_deploys,
    read_deploys,
    read_logs,
    read_metrics,
)

ROOT = Path(__file__).resolve().parent.parent.parent.parent


class WorkerAgent(ABC):
    """Base class for specialist worker agents.

    Each subclass specializes in one data domain and produces
    a validated ``WorkerFinding`` output. Workers use the existing
    tool functions to read incident data — they do not duplicate
    data-reading logic.
    """

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        self.finding: WorkerFinding | None = None

    @abstractmethod
    def observe(self) -> dict[str, Any]:
        """Read all available data for this worker's domain."""
        ...

    @abstractmethod
    def analyze(self, observation: dict[str, Any]) -> WorkerFinding:
        """Analyze observed data and produce a validated WorkerFinding."""
        ...

    def run(self) -> WorkerFinding:
        """Execute the worker pipeline: observe → analyze.

        Returns a validated ``WorkerFinding``.
        """
        observation = self.observe()
        self.finding = self.analyze(observation)
        return self.finding


class LogWorker(WorkerAgent):
    """Specialist worker for log analysis.

    Reads log files, extracts ERROR-level entries, identifies
    error patterns, and produces structured log findings.

    Responsibility (per OpenSpec): parse and summarize log data
    for anomalies. Reads log store. Produces structured findings.
    """

    def observe(self) -> dict[str, Any]:
        """Read all log data for the incident."""
        return {
            "logs": read_logs(self.incident_id),
            "error_logs": find_error_logs(self.incident_id),
        }

    def analyze(self, observation: dict[str, Any]) -> WorkerFinding:
        """Analyze logs and produce a WorkerFinding."""
        logs = observation["logs"]
        error_logs = observation["error_logs"]

        evidence: list[EvidenceItem] = []
        reasoning_steps: list[ReasoningStep] = []

        # Build evidence from ERROR-level log entries
        for err in error_logs[:5]:
            evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.LOG_ENTRY,
                    source=f"log:{err.timestamp}",
                    detail=f"LOG [{err.timestamp}] [{err.level}] {err.message}",
                    timestamp=err.timestamp,
                    confidence_weight=0.9,
                )
            )

        # Add all log entries as lower-weight evidence
        for log in logs[:3]:
            if log not in error_logs:
                evidence.append(
                    EvidenceItem(
                        source_type=EvidenceType.LOG_ENTRY,
                        source=f"log:{log.timestamp}",
                        detail=f"LOG [{log.timestamp}] [{log.level}] {log.message}",
                        timestamp=log.timestamp,
                        confidence_weight=0.3,
                    )
                )

        # Build reasoning steps
        service = logs[0].service if logs else "unknown"
        reasoning_steps.append(
            ReasoningStep(
                step_number=1,
                description=f"Read {len(logs)} log entries for service {service}",
                evidence_refs=[f"log:{err.timestamp}" for err in error_logs[:3]],
            )
        )
        if error_logs:
            unique_errors = {e.message for e in error_logs}
            reasoning_steps.append(
                ReasoningStep(
                    step_number=2,
                    description=f"Identified {len(unique_errors)} unique error pattern(s)",
                    evidence_refs=[f"log:{err.timestamp}" for err in error_logs[:3]],
                )
            )

        # Determine confidence based on error count
        confidence = min(0.5 + len(error_logs) * 0.1, 0.95)

        summary = (
            f"Log analysis for {self.incident_id}: found "
            f"{len(error_logs)} ERROR-level entries "
            f"across {len(logs)} total log entries "
            f"for service {service}"
        )

        return WorkerFinding(
            worker_type="log",
            incident_id=self.incident_id,
            evidence=evidence,
            reasoning_steps=reasoning_steps,
            confidence=confidence,
            summary=summary,
        )


class MetricsWorker(WorkerAgent):
    """Specialist worker for metric analysis.

    Reads metric time series, detects anomalies using statistical
    thresholds, and produces structured metric findings.

    Responsibility (per OpenSpec): analyze metric time series for
    anomalies/correlations. Reads metrics store. Produces structured findings.
    """

    def observe(self) -> dict[str, Any]:
        """Read all metric data for the incident."""
        return {
            "metrics": read_metrics(self.incident_id),
            "anomalies": self._detect_anomalies(),
        }

    def _detect_anomalies(self) -> dict[str, list[str]]:
        """Detect anomaly timestamps per metric."""
        metrics = read_metrics(self.incident_id)
        anomalies: dict[str, list[str]] = {}
        for metric_name, series in metrics.items():
            if not series:
                continue
            baseline = [p["value"] for p in series[: max(1, len(series) // 5)]]
            baseline_avg = sum(baseline) / len(baseline)
            baseline_stdev = (sum((v - baseline_avg) ** 2 for v in baseline) / len(baseline)) ** 0.5
            cutoff = baseline_avg + max(3 * baseline_stdev, baseline_avg * 2)
            anomaly_ts = [p["ts"] for p in series if p["value"] > cutoff]
            if anomaly_ts:
                anomalies[metric_name] = anomaly_ts[:3]
        return anomalies

    def analyze(self, observation: dict[str, Any]) -> WorkerFinding:
        """Analyze metrics and produce a WorkerFinding."""
        metrics = observation["metrics"]
        anomalies = observation["anomalies"]

        evidence: list[EvidenceItem] = []
        reasoning_steps: list[ReasoningStep] = []

        # Build evidence from anomalies
        for metric_name, timestamps in anomalies.items():
            for ts in timestamps:
                metric_data = metrics.get(metric_name, [])
                value = next(
                    (p["value"] for p in metric_data if p["ts"] == ts),
                    0.0,
                )
                evidence.append(
                    EvidenceItem(
                        source_type=EvidenceType.METRIC_ANOMALY,
                        source=f"metric:{metric_name}",
                        detail=f"METRIC {metric_name} anomaly at {ts} (value={value})",
                        timestamp=ts,
                        confidence_weight=0.85,
                    )
                )

        # Build reasoning steps
        metric_names = list(metrics.keys())
        reasoning_steps.append(
            ReasoningStep(
                step_number=1,
                description=f"Analyzed {len(metric_names)} metric series for {self.incident_id}",
                evidence_refs=[],
            )
        )
        if anomalies:
            anomaly_count = sum(len(v) for v in anomalies.values())
            reasoning_steps.append(
                ReasoningStep(
                    step_number=2,
                    description=(
                        f"Detected anomalies in {len(anomalies)} metric(s) with {anomaly_count} total anomaly points"
                    ),
                    evidence_refs=[f"metric:{m}" for m in anomalies],
                )
            )

        # Determine confidence based on anomaly count
        confidence = min(0.5 + sum(len(v) for v in anomalies.values()) * 0.1, 0.95)

        summary = (
            f"Metrics analysis for {self.incident_id}: detected "
            f"anomalies in {len(anomalies)} metric(s) "
            f"across {sum(len(v) for v in anomalies.values())} "
            f"data points"
        )

        return WorkerFinding(
            worker_type="metrics",
            incident_id=self.incident_id,
            evidence=evidence,
            reasoning_steps=reasoning_steps,
            confidence=confidence,
            summary=summary,
        )


class DeployHistoryWorker(WorkerAgent):
    """Specialist worker for deploy history analysis.

    Reads deploy records, correlates incident timing with recent
    deploys, and produces structured deploy findings.

    Responsibility (per OpenSpec): correlate incident timing with
    recent deploys/config changes. Reads deploy history store.
    Produces structured findings.
    """

    def observe(self) -> dict[str, Any]:
        """Read all deploy data for the incident."""
        return {
            "deploys": read_deploys(self.incident_id),
            "recent_deploys": get_recent_deploys(self.incident_id),
            "meta": self._read_meta(),
        }

    def _read_meta(self) -> dict:
        """Read incident metadata."""
        import json

        path = ROOT / "data" / "incidents" / self.incident_id / "meta.json"
        return json.loads(path.read_text())

    def analyze(self, observation: dict[str, Any]) -> WorkerFinding:
        """Analyze deploy history and produce a WorkerFinding."""
        deploys = observation["deploys"]
        recent_deploys = observation["recent_deploys"]
        meta = observation["meta"]

        evidence: list[EvidenceItem] = []
        reasoning_steps: list[ReasoningStep] = []

        # Build evidence from recent deploys
        for dep in recent_deploys[:3]:
            evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.DEPLOY,
                    source=f"deploy:{dep['deploy_id']}",
                    detail=f"DEPLOY {dep['deploy_id']}: {dep['description']}",
                    timestamp=dep.get("timestamp", ""),
                    confidence_weight=0.5,
                )
            )

        # Build reasoning steps
        service = meta["services"][0] if meta["services"] else "unknown"
        reasoning_steps.append(
            ReasoningStep(
                step_number=1,
                description=f"Read {len(deploys)} deploy records for {service}",
                evidence_refs=[f"deploy:{d['deploy_id']}" for d in recent_deploys[:3]],
            )
        )
        if recent_deploys:
            reasoning_steps.append(
                ReasoningStep(
                    step_number=2,
                    description=f"Found {len(recent_deploys)} deploy(s) before incident window for {service}",
                    evidence_refs=[f"deploy:{d['deploy_id']}" for d in recent_deploys[:3]],
                )
            )

        # Determine confidence based on deploy correlation
        confidence = min(0.5 + len(recent_deploys) * 0.1, 0.95)

        summary = (
            f"Deploy analysis for {self.incident_id}: found "
            f"{len(recent_deploys)} deploy(s) before the "
            f"incident window for service {service}"
        )

        return WorkerFinding(
            worker_type="deploy",
            incident_id=self.incident_id,
            evidence=evidence,
            reasoning_steps=reasoning_steps,
            confidence=confidence,
            summary=summary,
        )
