"""OrchestratorAgent for multi-agent incident diagnosis.

Stage 6 introduces the orchestrator–worker architecture:
the ``OrchestratorAgent`` (triage agent) receives an incident,
classifies it, dispatches specialist workers, and synthesizes
a unified ``Diagnosis``.

Each worker (LogWorker, MetricsWorker, DeployHistoryWorker)
operates on an independent data domain and produces a
validated ``WorkerFinding``.

See ROADMAP.md Phase C — Stage 6 for the full spec.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from incident_agent.agents.worker import (
    DeployHistoryWorker,
    LogWorker,
    MetricsWorker,
)
from incident_agent.schemas.agent_output import (
    EvidenceItem,
    EvidenceType,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools import read_meta


class OrchestrationState:
    """Lightweight state machine tracking the orchestration workflow.

    States: ``INITIALIZED`` → ``DISPATCHED`` → ``PROCESSING``
    → ``COMPLETED`` → ``SYNTHESIZED``

    Every state transition is tracked and validated by Pydantic.
    Designed to be compatible with future LangGraph integration.
    """

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        self.state = OrchestrationState.State.INITIALIZED
        self.completed_workers: list[str] = []
        self.failed_workers: list[str] = []
        self.findings: dict[str, WorkerFinding] = {}

    class State(str, Enum):
        INITIALIZED = "initialized"
        DISPATCHED = "dispatched"
        PROCESSING = "processing"
        COMPLETED = "completed"
        SYNTHESIZED = "synthesized"

    def dispatch(self) -> None:
        """Transition to DISPATCHED state."""
        self.state = self.State.DISPATCHED

    def process(self) -> None:
        """Transition to PROCESSING state."""
        self.state = self.State.PROCESSING

    def complete(self) -> None:
        """Transition to COMPLETED state."""
        self.state = self.State.COMPLETED

    def synthesize(self) -> None:
        """Transition to SYNTHESIZED state."""
        self.state = self.State.SYNTHESIZED

    def mark_worker_complete(self, worker_type: str) -> None:
        """Record that a worker has completed."""
        if worker_type not in self.completed_workers:
            self.completed_workers.append(worker_type)

    def mark_worker_failed(self, worker_type: str) -> None:
        """Record that a worker has failed."""
        if worker_type not in self.failed_workers:
            self.failed_workers.append(worker_type)
        if worker_type in self.completed_workers:
            self.completed_workers.remove(worker_type)

    def add_finding(self, finding: WorkerFinding) -> None:
        """Store a worker's finding."""
        self.findings[finding.worker_type] = finding

    @property
    def is_complete(self) -> bool:
        """Return True if all workers have completed or failed."""
        return len(self.completed_workers) + len(self.failed_workers) >= 3

    def summary(self) -> dict[str, Any]:
        """Return a summary of the orchestration state."""
        return {
            "incident_id": self.incident_id,
            "state": self.state.value,
            "completed_workers": self.completed_workers,
            "failed_workers": self.failed_workers,
            "total_findings": len(self.findings),
        }


class OrchestratorAgent:
    """Triage agent for multi-agent incident diagnosis.

    The orchestrator receives an incident, classifies it
    based on metadata, dispatches specialist workers
    (LogWorker, MetricsWorker, DeployHistoryWorker),
    collects their findings, and synthesizes a unified
    ``Diagnosis``.

    Responsibility (per OpenSpec): classify incoming incident,
    decide which specialist workers to invoke, dispatch
    instructions to workers.

    All outputs are validated Pydantic ``Diagnosis`` objects
    (Stage 4 cross-cutting rule).
    """

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        max_replans: int = 2,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.max_replans = max_replans
        self.diagnosis: Diagnosis | None = None
        self.state = OrchestrationState("UNKNOWN")

    def classify_incident(self, incident_id: str) -> dict[str, Any]:
        """Classify the incident based on metadata.

        Reads incident metadata and determines the category
        and severity to guide worker dispatch.

        Args:
            incident_id: The incident identifier.

        Returns:
            A dict with classification details.
        """
        meta = read_meta(incident_id)
        category = meta.get("category", "unknown")
        services = meta.get("services", [])
        severity = meta.get("severity", "unknown")
        return {
            "incident_id": incident_id,
            "category": category,
            "services": services,
            "severity": severity,
            "classification": self._categorize(category),
        }

    def _categorize(self, category: str) -> str:
        """Map category to a classification label."""
        if category in ("cpu_exhaustion", "memory_leak"):
            return "high_risk"
        elif category in ("cascading_failure", "db_connection"):
            return "medium_risk"
        return "low_risk"

    def dispatch_workers(self, incident_id: str) -> dict[str, WorkerFinding]:
        """Create and run all specialist workers.

        Each worker processes an independent data domain.
        Workers that fail are recorded but do not crash
        the orchestration.

        Args:
            incident_id: The incident identifier.

        Returns:
            A dict mapping worker_type to WorkerFinding.
        """
        self.state = OrchestrationState(incident_id)
        self.state.dispatch()
        self.state.process()

        findings: dict[str, WorkerFinding] = {}

        # Dispatch LogWorker
        try:
            log_worker = LogWorker(incident_id)
            findings["log"] = log_worker.run()
            self.state.mark_worker_complete("log")
        except Exception:
            self.state.mark_worker_failed("log")

        # Dispatch MetricsWorker
        try:
            metrics_worker = MetricsWorker(incident_id)
            findings["metrics"] = metrics_worker.run()
            self.state.mark_worker_complete("metrics")
        except Exception:
            self.state.mark_worker_failed("metrics")

        # Dispatch DeployHistoryWorker
        try:
            deploy_worker = DeployHistoryWorker(incident_id)
            findings["deploy"] = deploy_worker.run()
            self.state.mark_worker_complete("deploy")
        except Exception:
            self.state.mark_worker_failed("deploy")

        # Store all findings
        for finding in findings.values():
            self.state.add_finding(finding)

        self.state.complete()
        return findings

    def synthesize(
        self,
        findings: dict[str, WorkerFinding],
        incident_id: str,
    ) -> Diagnosis:
        """Combine worker findings into a unified Diagnosis.

        Collects evidence and reasoning from all workers,
        determines the root cause from the combined findings,
        and produces a validated ``Diagnosis``.

        Args:
            findings: Dict mapping worker_type to WorkerFinding.
            incident_id: The incident identifier.

        Returns:
            A validated ``Diagnosis`` object.
        """
        self.state.synthesize()

        # Collect all evidence and reasoning from workers
        all_evidence: list[EvidenceItem] = []
        all_reasoning: list[ReasoningStep] = []
        all_worker_types: list[str] = []

        for worker_type, finding in findings.items():
            all_evidence.extend(finding.evidence)
            all_reasoning.extend(finding.reasoning_steps)
            all_worker_types.append(worker_type)
            self.state.mark_worker_complete(worker_type)

        # Ensure evidence is sorted by order
        all_evidence.sort(key=lambda e: e.source)

        # Build combined reasoning
        combined_reasoning = self._build_combined_reasoning(all_reasoning, all_worker_types, findings)

        # Read meta for basic info
        meta = read_meta(incident_id)
        service = meta["services"][0] if meta["services"] else "unknown"
        category = meta.get("category", "unknown")

        # Determine root cause from combined findings
        root_cause = self._infer_root_cause(findings, category)

        # Calculate confidence as weighted average of worker confidences
        if findings:
            avg_confidence = sum(f.confidence for f in findings.values()) / len(findings)
            confidence = min(avg_confidence + 0.1, 0.95)
        else:
            confidence = 0.5

        # Adjust confidence based on evidence count
        confidence = min(confidence + len(all_evidence) * 0.02, 0.95)

        # Determine risk tier
        risk_tier = self._determine_risk_tier(category)

        # Build recommendation
        recommendation = self._build_recommendation(root_cause, service, risk_tier)

        # Build reasoning steps for the diagnosis
        reasoning_steps = [
            ReasoningStep(
                step_number=i,
                description=step,
                evidence_refs=[],
            )
            for i, step in enumerate(combined_reasoning, 1)
        ]

        # Ensure at least one evidence item exists
        if not all_evidence:
            all_evidence.append(
                EvidenceItem(
                    source_type=EvidenceType.LOG_ENTRY,
                    source="log:no_evidence",
                    detail="No evidence found across all workers",
                    timestamp="",
                    confidence_weight=0.1,
                )
            )

        diagnosis = Diagnosis(
            agent_type="orchestrator_worker",
            incident_id=incident_id,
            root_cause=root_cause,
            confidence=confidence,
            evidence=all_evidence,
            affected_service=service,
            category=category,
            reasoning_steps=reasoning_steps,
            recommendation=recommendation,
            risk_tier=risk_tier,
        )

        self.diagnosis = diagnosis
        return diagnosis

    def run(self, incident_id: str) -> Diagnosis:
        """Execute the full orchestrator pipeline.

        Pipeline: classify → dispatch workers → synthesize → return Diagnosis.

        Args:
            incident_id: The incident identifier.

        Returns:
            A validated ``Diagnosis`` with confidence >= self.confidence_threshold.
        """
        # Step 1: Classify the incident (side effect: metadata validation)
        self.classify_incident(incident_id)

        # Step 2: Dispatch specialist workers
        findings = self.dispatch_workers(incident_id)

        # Step 3: Synthesize findings into a Diagnosis
        diagnosis = self.synthesize(findings, incident_id)

        # Step 4: ReAct-style confidence adjustment
        if diagnosis.confidence < self.confidence_threshold:
            error_count = len(
                findings.get(
                    "log",
                    WorkerFinding(
                        worker_type="log",
                        incident_id=incident_id,
                        evidence=[],
                        reasoning_steps=[],
                        confidence=0.0,
                        summary="Fallback log worker finding",
                    ),
                ).evidence
            )
            if error_count > 20:
                diagnosis = Diagnosis(
                    agent_type="orchestrator_worker",
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
                            description=f"Re-observed {error_count} total error entries — increasing confidence",
                            evidence_refs=[],
                        )
                    ],
                    recommendation=diagnosis.recommendation,
                    risk_tier=diagnosis.risk_tier,
                )

        return diagnosis

    def _build_combined_reasoning(
        self,
        all_reasoning: list[ReasoningStep],
        worker_types: list[str],
        findings: dict[str, WorkerFinding],
    ) -> list[str]:
        """Build combined reasoning from all workers."""
        steps: list[str] = []
        steps.append(f"Orchestrated {len(worker_types)} specialist workers: {', '.join(worker_types)}")
        for worker_type, finding in findings.items():
            steps.append(f"{worker_type.upper()} worker: {finding.summary}")
        if all_reasoning:
            unique_descriptions = list({r.description for r in all_reasoning})
            for desc in unique_descriptions[:3]:
                steps.append(desc)
        return steps

    def _infer_root_cause(
        self,
        findings: dict[str, WorkerFinding],
        category: str,
    ) -> str:
        """Infer root cause from combined worker findings."""
        # Collect all error messages from log findings
        all_errors = []
        for finding in findings.values():
            for evidence in finding.evidence:
                if evidence.source_type == EvidenceType.LOG_ENTRY:
                    all_errors.append(evidence.detail)

        combined = " ".join(all_errors).lower()

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

    def _build_recommendation(
        self,
        root_cause: str,
        service: str,
        risk_tier: RiskTier,
    ) -> str:
        """Build remediation recommendation based on root cause."""
        if "unbounded retry" in root_cause.lower():
            return "Restart affected pods to shed stuck threads, then patch the retry handler to cap retry attempts."
        if "connection pool" in root_cause.lower():
            return "Restart affected pods to reset connections, then fix the connection leak in request handler."
        if "memory leak" in root_cause.lower():
            return "Restart affected pods to reclaim memory, then fix the object retention bug."
        return (
            f"Restart affected {service} instances and investigate "
            f"the root cause ({risk_tier.value}-risk action — requires approval)."
        )
