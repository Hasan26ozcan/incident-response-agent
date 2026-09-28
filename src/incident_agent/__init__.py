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

Stage 7: Hierarchical swarm — an Incident Commander agent sits above
the orchestrator, synthesizing worker findings into a unified incident
narrative with escalation decisions.

Stage 8: Debate mechanism — a root-cause agent argues for the diagnosis
while a forensic examiner agent challenges it, producing a
before/after false-positive rate comparison and a final verdict.

Stage 9: Tree-of-Thought + Plan-and-Solve — parallel hypothesis
branching generates multiple root-cause explanations, scores them
independently, and selects the most likely scenario with
Plan-and-Solve validation.

Stage 10: Vector DB & Hybrid Retrieval — BM25 + Dense retrieval
with Reciprocal Rank Fusion (RRF). Knowledge-grounded incident
retrieval for evidence-based diagnosis.

Stage 11: Cross-encoder re-ranking — RerankerAgent re-ranks hybrid
retrieval results using a cross-encoder that scores query-document
pairs jointly. Includes before/after MRR and Precision@K metrics.

See:
  - incident_agent.schemas — Pydantic output schemas (Stage 4)
  - incident_agent.prompts — Prompt library (Stage 4)
  - incident_agent.agents.react_agent — ReAct agent (Stage 3→5)
  - incident_agent.agents.orchestrator — OrchestratorAgent (Stage 6)
  - incident_agent.agents.worker — Specialist worker agents (Stage 6)
  - incident_agent.agents.incident_commander — IncidentCommander (Stage 7)
  - incident_agent.workflows.plan — Diagnostic plan with replanning (Stage 5)
  - incident_agent.agents.tree_of_thought_agent — TreeOfThoughtAgent (Stage 9)
  - incident_agent.agents.retrieval_agent — RetrievalAgent (Stage 10)
  - incident_agent.agents.reranker_agent — RerankerAgent (Stage 11)
"""

__version__ = "0.1.0"

from incident_agent.agents.debate_mechanism import DebateMechanism
from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import (
    OrchestrationState,
    OrchestratorAgent,
)
from incident_agent.agents.react_agent import Diagnosis, ReActAgent
from incident_agent.agents.reranker_agent import RerankerAgent
from incident_agent.agents.retrieval_agent import RetrievalAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
from incident_agent.agents.tree_of_thought_agent import TreeOfThoughtAgent
from incident_agent.agents.worker import (
    DeployHistoryWorker,
    LogWorker,
    MetricsWorker,
    WorkerAgent,
)
from incident_agent.schemas import (
    AgentOutput,
    Argument,
    CommanderDiagnosis,
    DebateOutcome,
    EvidenceItem,
    HybridSearchResult,
    Hypothesis,
    IncidentMetadata,
    MetricAnomaly,
    ReasoningStep,
    RelevanceDelta,
    RerankedResult,
    RiskTier,
    SearchHit,
    TreeOfThoughtResult,
    WorkerFinding,
)
from incident_agent.workflows.plan import Plan, StepFailure

__all__ = [
    "__version__",
    "Diagnosis",
    "ReActAgent",
    "OrchestratorAgent",
    "OrchestrationState",
    "IncidentCommander",
    "CommanderDiagnosis",
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
    "Argument",
    "DebateOutcome",
    "RootCauseAgent",
    "ForensicExaminerAgent",
    "DebateMechanism",
    "TreeOfThoughtAgent",
    "Hypothesis",
    "TreeOfThoughtResult",
    "SearchHit",
    "HybridSearchResult",
    "RelevanceDelta",
    "RerankedResult",
    "RetrievalAgent",
    "RerankerAgent",
]
