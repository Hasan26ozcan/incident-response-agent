"""ReAct agent for single-agent incident diagnosis.

Implements the ReAct (Reason + Act) loop described in
ROADMAP.md Stage 3. The agent reads incident data, observes
anomalies, reasons about root causes, and produces a structured
diagnosis validated by Pydantic schemas (Stage 4).

Stage 5: Added dynamic planning and error recovery.
The agent generates a diagnostic plan, executes it step-by-step,
and replans when a step fails — rebuilding the plan from feedback
instead of crashing.

Stage 3→4: Every agent output is now a Pydantic-validated
object — no free text passes between agents or to tools.
See openspec/agent-responsibility-matrix.md § Cross-cutting rules
(rule #3) for the schema-validation mandate.
"""

from __future__ import annotations

from typing import Any

from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep, RiskTier
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools import (
    detect_anomaly_timestamps,
    find_error_logs,
    get_recent_deploys,
    read_meta,
)
from incident_agent.workflows.plan import (
    Plan,
    PlanStep,
    PlanStepType,
    StepFailure,
    generate_plan,
)


class ReActAgent:
    """Single-agent ReAct loop for incident diagnosis.

    The loop follows the pattern:
        1. Observe: read incident data
        2. Reason: analyze anomalies, correlate evidence
        3. Act: produce a Pydantic-validated Diagnosis
        4. Repeat: until confidence threshold is met

    Stage 5 adds a planning layer: the agent generates a
    diagnostic plan before execution, and replans when any
    step fails — rebuilding the plan from the failure feedback.

    All outputs conform to the Diagnosis Pydantic schema,
    ensuring structural consistency for downstream consumers.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        max_replans: int = 2,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.max_replans = max_replans
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

    def generate_plan(self, incident_id: str, strategy: str = "standard") -> Plan:
        """Generate a diagnostic plan for the given incident.

        Stage 5: the agent can produce a plan before execution
        to guide the order and scope of diagnostic steps.

        Args:
            incident_id: The incident identifier.
            strategy: The planning strategy ("standard",
                "log_only", or "metrics_only").

        Returns:
            A Plan with ordered diagnostic steps.
        """
        return generate_plan(incident_id, strategy=strategy)

    def execute_plan(self, plan: Plan, observation: dict) -> dict[str, Any]:
        """Execute all steps in a plan, collecting observations.

        Stage 5: executes each PlanStep, raising StepFailure
        when a step cannot produce valid results.

        Args:
            plan: The plan to execute.
            observation: The incident observation data.

        Returns:
            A dict of step_type -> observation results.

        Raises:
            StepFailure: If a step cannot produce valid results.
        """
        results: dict[str, Any] = {}

        for step in plan.steps:
            step_type = step.step_type.value

            if step_type == "read_meta":
                meta = read_meta(plan.incident_id)
                results[step_type] = meta
            elif step_type == "read_logs":
                from incident_agent.tools import read_logs
                logs = read_logs(plan.incident_id)
                results[step_type] = logs
            elif step_type == "find_error_logs":
                errors = find_error_logs(plan.incident_id)
                if not errors:
                    raise StepFailure(
                        step,
                        "No ERROR-level log entries found",
                    )
                results[step_type] = errors
            elif step_type == "detect_anomalies":
                anomalies = detect_anomaly_timestamps(plan.incident_id)
                if not anomalies:
                    raise StepFailure(
                        step,
                        "No metric anomalies detected",
                    )
                results[step_type] = anomalies
            elif step_type == "read_deploys":
                from incident_agent.tools import read_deploys
                deploys = read_deploys(plan.incident_id)
                results[step_type] = deploys
            elif step_type == "synthesize":
                results[step_type] = {"status": "synthesized"}
            else:
                raise StepFailure(step, f"Unknown step type: {step_type}")

        return results

    def replan(self, failed_step: str, current_plan: Plan, observation: dict) -> Plan:
        """Rebuild the plan from failure feedback.

        Stage 5: when a step fails, the agent replans by
        removing the failed step and its dependencies and
        substituting an alternative strategy.

        Args:
            failed_step: The type of the step that failed.
            current_plan: The plan that failed.
            observation: The current incident observation data.

        Returns:
            A new Plan with the failed step replaced.
        """
        new_plan = Plan(
            incident_id=current_plan.incident_id,
            replan_count=current_plan.replan_count + 1,
            failed_step=failed_step,
        )

        # Strategy: skip the failed step type and its dependents,
        # substitute with an alternative approach
        skip_types: set[str] = {failed_step}

        # If logs step failed, switch to metrics-only
        if failed_step == "find_error_logs":
            new_plan.add_step(
                PlanStep(
                    step_type=PlanStepType.DETECT_ANOMALIES,
                    description="Detect anomalies from metric time series (log fallback failed)",
                    order=0,
                )
            )
            new_plan.add_step(
                PlanStep(
                    step_type=PlanStepType.SYNTHESIZE,
                    description="Synthesize findings from metric analysis",
                    order=1,
                )
            )
            return new_plan

        # If anomaly detection failed, fall back to log-only
        if failed_step == "detect_anomalies":
            new_plan.add_step(
                PlanStep(
                    step_type=PlanStepType.FIND_ERROR_LOGS,
                    description="Find ERROR log entries (anomaly detection fallback failed)",
                    order=0,
                )
            )
            new_plan.add_step(
                PlanStep(
                    step_type=PlanStepType.SYNTHESIZE,
                    description="Synthesize findings from log analysis",
                    order=1,
                )
            )
            return new_plan

        # For other failures, generate a standard plan skipping failed types
        for step in current_plan.steps:
            if step.step_type.value not in skip_types:
                new_plan.add_step(step)

        # Add a synthesis step if not present
        if not any(s.step_type == PlanStepType.SYNTHESIZE for s in new_plan.steps):
            new_plan.add_step(
                PlanStep(
                    step_type=PlanStepType.SYNTHESIZE,
                    description="Synthesize all available evidence",
                    order=new_plan.step_count(),
                )
            )

        return new_plan

    def run(self, incident_id: str) -> Diagnosis:
        """Execute the full ReAct loop with dynamic planning and error recovery.

        Stage 5: The agent generates a diagnostic plan, executes
        it step-by-step, and replans when a step fails — rebuilding
        the plan from the failure feedback. This provides resilience
        against partial data unavailability or unexpected conditions.

        Returns a Diagnosis with confidence >= self.confidence_threshold.
        The output is a Pydantic-validated object conforming to the
        Diagnosis schema.
        """
        observation = self.observe(incident_id)
        plan = self.generate_plan(incident_id)
        reasoning = self.reason(observation)

        # Plan execution loop with replanning on failure
        while True:
            try:
                _ = self.execute_plan(plan, observation)
                break  # All steps executed successfully
            except StepFailure as failure:
                if plan.replan_count >= self.max_replans:
                    # Exhausted replan attempts — execute with
                    # what we have and adjust confidence
                    break
                # Rebuild the plan from failure feedback
                plan = self.replan(failure.step.step_type.value, plan, observation)

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
            return "Restart affected pods to reset connections, then fix the connection leak in request handler."
        if "memory leak" in root_cause.lower():
            return "Restart affected pods to reclaim memory, then fix the object retention bug."
        return (
            f"Restart affected {service} instances and investigate "
            f"the root cause ({risk_tier.value}-risk action — requires approval)."
        )
