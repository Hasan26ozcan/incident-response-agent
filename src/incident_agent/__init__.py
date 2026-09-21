"""Incident Response Agent — agentic incident triage and diagnosis.

This package is built up incrementally across the 23-stage roadmap in
ROADMAP.md. At Stage 4, all agent outputs are Pydantic-validated
objects with a prompt library supporting few-shot examples and
structured system prompts.

Stage 5: Dynamic planning and error recovery — agents can generate
diagnostic plans, execute them step-by-step, and replan when a step
fails.

Stage 6: Multi-agent orchestrator–workers architecture — a triage
agent dispatches specialist workers (logs, metrics, deploy history)
and synthesizes their findings into a unified Diagnosis.

See:
  - incident_agent.schemas — Pydantic output schemas (Stage 4)
  - incident_agent.prompts — Prompt library (Stage 4)
  - incident_agent.agents.react_agent — ReAct agent (Stage 3→5)
  - incident_agent.agents.orchestrator — OrchestratorAgent (Stage 6)
  - incident_agent.agents.worker — Specialist worker agents (Stage 6)
  - incident_agent.workflows.plan — Diagnostic plan with replanning (Stage 5)
"""

__version__ = "0.1.0"

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
from incident_agent.schemas import (
    AgentOutput,
    EvidenceItem,
    IncidentMetadata,
    MetricAnomaly,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.workflows.plan import Plan, StepFailure

__all__ = [
    "__version__",
    "Diagnosis",
    "ReActAgent",
    "OrchestratorAgent",
    "OrchestrationState",
    "LogWorker",
    "MetricsWorker",
    "DeployHistoryWorker",
    "WorkerAgent",
    "AgentOutput",
    "EvidenceItem",
    "IncidentMetadata",
    "MetricAnomaly",
    "ReasoningStep",
    "RiskTier",
    "WorkerFinding",
    "Plan",
    "StepFailure",
]
