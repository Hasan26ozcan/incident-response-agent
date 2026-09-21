# Proposal: Stage 6 — Orchestrator–Workers

- **Status:** Approved
- **Stage:** 6 of 23
- **Phase:** C — Multi-Agent Architecture
- **Depends on:** Stage 5 (Dynamic Planning & Error Recovery)

## 1. Problem / Motivation

At Stage 5, a single `ReActAgent` handles every aspect of incident diagnosis end-to-end — reading logs, metrics, and deploys in one sequential loop. While functional, this approach does not scale: as the number of data sources grows, a single agent becomes a bottleneck and cannot specialize its reasoning per data type.

Stage 6 introduces a **multi-agent orchestrator–worker architecture**. A lightweight `OrchestratorAgent` (the triage agent) receives an incident, classifies it, dispatches specialist workers in parallel, collects their findings, and synthesizes a unified `Diagnosis`. Each worker specializes in one data domain (logs, metrics, deploy history), producing structured, schema-validated output.

This stage is the foundation for more sophisticated architectures in Stage 7 (Hierarchical Swarm with Incident Commander) and beyond, where the orchestrator gains higher-level reasoning and delegation capabilities.

## 2. Scope

### In scope

- **Specialist worker agents** (each a Pydantic-validated output producer):
  - `LogWorker` — parses log files, extracts error patterns, produces structured log findings
  - `MetricsWorker` — analyzes metric time series, detects anomalies, produces structured metric findings
  - `DeployHistoryWorker` — correlates incident timing with recent deploys, produces structured deploy findings
- **OrchestratorAgent** (triage agent):
  - `classify_incident()` — reads metadata and determines incident category
  - `dispatch_workers()` — creates and runs specialist workers
  - `synthesize()` — combines worker outputs into a unified `Diagnosis`
  - `run()` — full orchestration pipeline: classify → dispatch → synthesize
- **OrchestrationState** — lightweight state machine tracking the workflow stage
  - States: `INITIALIZED` → `DISPATCHED` → `PROCESSING` → `COMPLETED` → `SYNTHESIZED`
  - Tracks which workers have completed and their outputs
- **Cross-cutting rule maintained**: every agent output is a validated Pydantic object (Stage 4 requirement)
- **Test suite**: comprehensive tests for worker agents, orchestrator, state management, and end-to-end diagnosis

### Out of scope

- Multi-agent debate (Stage 8)
- Hierarchical command structure (Stage 7 — Incident Commander)
- LLM integration (workers are tool-driven; LLM-based reasoning comes later)
- Temporal workflows (Stage 17+)

## 3. Design decisions

### 3.1 Worker specialization

**Decision**: Each worker handles exactly one data source domain.

**Rationale**:
- Keeps each worker focused and testable
- Allows independent development and replacement of workers
- Mirrors real-world incident response where specialists handle different data types
- Follows the OpenSpec agent responsibility matrix (Log Agent, Metrics Agent, Deploy-History Agent)

### 3.2 Orchestrator as triage agent

**Decision**: The `OrchestratorAgent` acts as a triage agent — it classifies the incident, dispatches workers, and synthesizes results. It does not directly read data itself.

**Rationale**:
- Separates coordination logic from data processing logic
- Makes the orchestrator lightweight and focused on workflow management
- The orchestrator delegates all data reading to workers, which use the existing tool functions
- Follows the OpenSpec responsibility matrix (Triage Agent reads metadata/alerts, dispatches to workers)

### 3.3 State management without external dependencies

**Decision**: `OrchestrationState` is a Pydantic model with an enum state machine — no external state management library.

**Rationale**:
- The project uses minimal dependencies (torch, numpy, pydantic); adding langgraph or other state libraries would break the incremental installation model
- The state machine is simple enough to be self-contained
- The design is compatible with future replacement by LangGraph (Stage 6 mentions "State management via LangGraph" but can start with a Pydantic state model)
- Every state transition is validated by Pydantic

### 3.4 Workers use existing tools

**Decision**: Workers call the existing tool functions (`read_logs`, `read_metrics`, `read_deploys`, `find_error_logs`, etc.) — they do not duplicate data-reading logic.

**Rationale**:
- Avoids code duplication
- Workers focus on analysis and pattern extraction, not data parsing
- Maintains backward compatibility: the same tool functions serve both the single-agent ReActAgent (Stage 3-5) and the multi-agent workers (Stage 6+)

### 3.5 Worker output schema

**Decision**: Each worker produces a `WorkerFinding` Pydantic model containing evidence items, reasoning steps, and a confidence score.

**Rationale**:
- Consistent with the Stage 4 cross-cutting rule (every agent output is a validated Pydantic object)
- `WorkerFinding` can be mapped to `EvidenceItem` and `ReasoningStep` for the final `Diagnosis`
- Provides a clear contract between workers and the orchestrator

## 4. Schema definitions

### 4.1 WorkerFinding

```python
class WorkerFinding(BaseModel):
    worker_type: str
    incident_id: str
    evidence: list[EvidenceItem]
    reasoning_steps: list[ReasoningStep]
    confidence: float  # 0.0 to 1.0
    summary: str
```

### 4.2 OrchestrationState

```python
class OrchestrationState(str, Enum):
    INITIALIZED = "initialized"
    DISPATCHED = "dispatched"
    PROCESSING = "processing"
    COMPLETED = "completed"
    SYNTHESIZED = "synthesized"

class OrchestrationStatus(BaseModel):
    incident_id: str
    state: OrchestrationState
    completed_workers: list[str] = Field(default_factory=list)
    failed_workers: list[str] = Field(default_factory=list)
    findings: dict[str, WorkerFinding] = Field(default_factory=dict)
```

## 5. Acceptance criteria

- [x] `WorkerFinding` Pydantic model with worker_type, evidence, reasoning_steps, confidence, summary
- [x] `LogWorker` — reads logs, extracts error patterns, produces `WorkerFinding`
- [x] `MetricsWorker` — reads metrics, detects anomalies, produces `WorkerFinding`
- [x] `DeployHistoryWorker` — reads deploys, correlates with incident window, produces `WorkerFinding`
- [x] `OrchestratorAgent` — classifies incident, dispatches workers, synthesizes Diagnosis
- [x] `OrchestrationState` — Pydantic state machine with all states
- [x] `OrchestrationStatus` — tracks progress, completed/failed workers, findings
- [x] `OrchestratorAgent.run("INC-001")` produces a schema-valid `Diagnosis`
- [x] `OrchestratorAgent.run()` dispatches all three workers
- [x] `OrchestratorAgent.run()` synthesizes worker findings into a unified `Diagnosis`
- [x] State transitions are tracked correctly through the full pipeline
- [x] `OrchestrationStatus` reflects completed and failed workers
- [x] Workers produce findings with at least one evidence item
- [x] ReActAgent remains available alongside new orchestrator (backward compatible)
- [x] All tests pass (Stage 6 + existing stages)
- [x] Cross-cutting rule verified: every agent output is a validated Pydantic object
- [x] Backward compatible: all Stage 3-5 tests still pass

## 6. Risks / open questions

- **Worker isolation**: Workers run sequentially in the orchestrator (not truly parallel). True parallelism would require asyncio or threading, which is deferred to Stage 17 (Temporal). The conceptual parallelism is preserved — each worker processes an independent data domain.
- **State persistence**: `OrchestrationState` is in-memory only. Persistent state storage comes in Stage 12 (episodic memory).
- **Worker failure handling**: If a worker fails (e.g., missing data), the orchestrator records it and continues with remaining workers. The orchestrator does not crash on a single worker failure.
- **LangGraph compatibility**: The roadmap mentions LangGraph for state management. The current Pydantic-based state machine is designed to be replaceable with a LangGraph graph in the future without changing the worker interfaces.
