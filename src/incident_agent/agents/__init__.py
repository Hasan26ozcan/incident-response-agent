"""Agent implementations.

Populated starting Stage 3 (single-agent ReAct skeleton),
extended through Stage 5 (dynamic planning and error recovery),
Stage 6 (multi-agent orchestrator–workers),
and Stage 7 (hierarchical swarm with Incident Commander).

Stage 7 introduces the Incident Commander agent, which sits
above the OrchestratorAgent in the hierarchy. It synthesizes
worker findings into a unified incident narrative with
escalation decisions.

Stage 8 introduces the debate mechanism:
a root-cause agent argues for the initial diagnosis
while a forensic examiner agent challenges it, producing
a before/after false-positive rate comparison and a final verdict.

Stage 4: All agent outputs are Pydantic-validated objects —
see incident_agent.schemas for the canonical schema definitions.

Stage 5: Dynamic planning and error recovery — agents can generate
diagnostic plans, execute them step-by-step, and replan when a step
fails. See incident_agent.workflows.plan for the plan data structures.
"""

from incident_agent.agents.debate_mechanism import DebateMechanism
from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import (
    OrchestrationState,
    OrchestratorAgent,
)
from incident_agent.agents.react_agent import Diagnosis, ReActAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
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
    "IncidentCommander",
    "LogWorker",
    "MetricsWorker",
    "DeployHistoryWorker",
    "WorkerAgent",
    "Plan",
    "StepFailure",
    "RootCauseAgent",
    "ForensicExaminerAgent",
    "DebateMechanism",
]
