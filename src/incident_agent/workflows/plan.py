"""Dynamic planning and error recovery for the incident diagnosis agent.

Stage 5 implements a replanning loop: when a diagnostic step fails,
the agent rebuilds its plan from the failure feedback instead of
crashing. This module defines the plan data structures, step-level
failure handling, and the replanning strategy.

See ROADMAP.md Phase B — Stage 5 for the full spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PlanStepType(str, Enum):
    """Types of diagnostic steps in a plan."""

    READ_META = "read_meta"
    READ_LOGS = "read_logs"
    FIND_ERROR_LOGS = "find_error_logs"
    DETECT_ANOMALIES = "detect_anomalies"
    READ_DEPLOYS = "read_deploys"
    SYNTHESIZE = "synthesize"


@dataclass(frozen=True)
class PlanStep:
    """A single step in a diagnostic plan."""

    step_type: PlanStepType
    description: str
    order: int


class StepFailure(Exception):
    """Raised when a diagnostic step fails during plan execution.

    Carries context about which step failed and why, so the
    replanner can use that feedback to build a better plan.
    """

    def __init__(
        self,
        step: PlanStep,
        reason: str,
    ) -> None:
        self.step = step
        self.reason = reason
        super().__init__(f"Step '{step.step_type.value}' failed: {reason}")


@dataclass
class Plan:
    """A sequence of diagnostic steps to execute for an incident.

    The plan can be replanned when a step fails — replacing the
    failed step and any dependent steps with an alternative strategy.
    """

    incident_id: str
    steps: list[PlanStep] = field(default_factory=list)
    replan_count: int = 0
    failed_step: str = ""

    def add_step(self, step: PlanStep) -> None:
        """Append a step to the plan."""
        self.steps.append(step)

    def get_step(self, index: int) -> PlanStep:
        """Return the step at the given index."""
        return self.steps[index]

    def is_empty(self) -> bool:
        """Return True if the plan has no steps."""
        return len(self.steps) == 0

    def step_count(self) -> int:
        """Return the number of steps in the plan."""
        return len(self.steps)

    def mark_failed(self, step_type: str) -> None:
        """Record that a step of the given type failed."""
        self.failed_step = step_type
        self.replan_count += 1


def generate_plan(incident_id: str, strategy: str = "standard") -> Plan:
    """Generate a diagnostic plan for the given incident.

    Args:
        incident_id: The incident identifier (e.g. "INC-001").
        strategy: The planning strategy — "standard",
            "log_only", or "metrics_only".

    Returns:
        A Plan with steps ordered for execution.
    """
    plan = Plan(incident_id=incident_id)

    if strategy == "log_only":
        plan.add_step(
            PlanStep(
                step_type=PlanStepType.FIND_ERROR_LOGS,
                description="Scan for ERROR-level entries in logs",
                order=0,
            )
        )
        plan.add_step(
            PlanStep(
                step_type=PlanStepType.SYNTHESIZE,
                description="Synthesize findings from log analysis",
                order=1,
            )
        )
        return plan

    if strategy == "metrics_only":
        plan.add_step(
            PlanStep(
                step_type=PlanStepType.DETECT_ANOMALIES,
                description="Detect metric anomalies from time series",
                order=0,
            )
        )
        plan.add_step(
            PlanStep(
                step_type=PlanStepType.SYNTHESIZE,
                description="Synthesize findings from metric analysis",
                order=1,
            )
        )
        return plan

    # Standard strategy: read everything
    plan.add_step(
        PlanStep(
            step_type=PlanStepType.READ_META,
            description="Read incident metadata",
            order=0,
        )
    )
    plan.add_step(
        PlanStep(
            step_type=PlanStepType.READ_LOGS,
            description="Read and parse log files",
            order=1,
        )
    )
    plan.add_step(
        PlanStep(
            step_type=PlanStepType.FIND_ERROR_LOGS,
            description="Filter for ERROR-level log entries",
            order=2,
        )
    )
    plan.add_step(
        PlanStep(
            step_type=PlanStepType.DETECT_ANOMALIES,
            description="Detect anomalies in metric time series",
            order=3,
        )
    )
    plan.add_step(
        PlanStep(
            step_type=PlanStepType.READ_DEPLOYS,
            description="Read recent deploys before incident window",
            order=4,
        )
    )
    plan.add_step(
        PlanStep(
            step_type=PlanStepType.SYNTHESIZE,
            description="Synthesize all evidence into root-cause diagnosis",
            order=5,
        )
    )

    return plan
