"""Durable workflow orchestration.

Populated starting Stage 5 (dynamic planning and error recovery)
and Stage 17 (Temporal workflow for long-running incidents
that survive interruption). Stage 5 adds a plan-based execution
model where the agent generates a diagnostic plan, executes it
step-by-step, and replans when a step fails.

See ROADMAP.md Phase B (Stage 5) and Phase F (Stage 17) and
openspec/risk-classification.md for the approval-gate rules this
package must enforce for every medium/high-risk action.
"""

from incident_agent.workflows.plan import Plan, PlanStep, StepFailure, generate_plan

__all__ = [
    "Plan",
    "PlanStep",
    "StepFailure",
    "generate_plan",
]
