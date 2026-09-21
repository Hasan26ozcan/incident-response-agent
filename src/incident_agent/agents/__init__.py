"""Agent implementations.

Populated starting Stage 3 (single-agent ReAct skeleton),
extended through Stage 5 (dynamic planning and error recovery),
and Stage 6 (multi-agent orchestrator–workers).

Stage 6 introduces the orchestrator–worker architecture:
a triage agent dispatches specialist workers (logs, metrics,
deploy history) and synthesizes their findings into a
unified Diagnosis.

See ROADMAP.md Phase C and openspec/agent-responsibility-matrix.md
for what each agent introduced here is responsible for and what
risk tier its actions fall under.

Stage 4: All agent outputs are Pydantic-validated objects —
see incident_agent.schemas for the canonical schema definitions.

Stage 5: Dynamic planning and error recovery — agents can generate
diagnostic plans, execute them step-by-step, and replan when a step
fails. See incident_agent.workflows.plan for the plan data structures.
"""

from incident_agent.agents.orchestrator import (
    OrchestrationState,
    OrchestratorAgent,
)
from incident_agent.agents.react_agent import Diagnosis, ReActAgent
from incident_agent.agents.worker import (
    DeployHistoryWorker,
    LogWorker,
    MetricsWorker,
    WorkerAgent,
)
from incident_agent.workflows.plan import Plan, StepFailure

__all__ = [
    "ReActAgent",
    "Diagnosis",
    "OrchestratorAgent",
    "OrchestrationState",
    "LogWorker",
    "MetricsWorker",
    "DeployHistoryWorker",
    "WorkerAgent",
    "Plan",
    "StepFailure",
]
