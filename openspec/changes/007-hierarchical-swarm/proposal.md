# Proposal: Stage 7 — Hierarchical Swarm

- **Status:** Approved
- **Stage:** 7 of 23
- **Phase:** C — Multi-Agent Architecture
- **Depends on:** Stage 6 (Orchestrator–Workers)

## 1. Problem / Motivation

At Stage 6, the `OrchestratorAgent` dispatches specialist workers and synthesizes their findings into a `Diagnosis`. While this multi-agent architecture is functional, it lacks a strategic oversight layer — there is no agent that steps back, reads the combined picture from all specialists, and makes an escalation decision.

Stage 7 introduces the **Incident Commander** agent, which sits above the orchestrator in the hierarchy. The Commander receives the orchestrator's `Diagnosis` and all `WorkerFinding` outputs, then produces a `CommanderDiagnosis` containing a unified incident narrative, an escalation decision, and a severity assessment.

This stage establishes the hierarchical swarm pattern that enables more sophisticated multi-agent architectures in later stages (Stages 8–9 with debate and Tree-of-Thought).

## 2. Scope

### In scope

- **`IncidentCommander` class**:
  - `synthesize_narrative()` — builds a unified incident narrative from diagnosis and worker findings
  - `determine_escalation()` — makes escalation decision (escalate / monitor / resolve) based on risk tier and confidence
  - `assess_severity()` — evaluates overall severity from risk tier, evidence count, and worker coverage
  - `run()` — full commander pipeline: narrative → escalation → severity → reasoning → CommanderDiagnosis
- **`CommanderDiagnosis` Pydantic model**:
  - Extends `AgentOutput` with narrative, escalation decision, severity assessment, and underlying diagnosis
  - Contains the full `Diagnosis` object and all `WorkerFinding` objects as nested validated Pydantic objects
- **Cross-cutting rule maintained**: every agent output is a validated Pydantic object (Stage 4 requirement)
- **Test suite**: comprehensive tests for the Incident Commander, CommanderDiagnosis schema, and end-to-end commander pipeline

### Out of scope

- Remediation actions (Stage 16+)
- Debate mechanism (Stage 8)
- Tree-of-Thought branching (Stage 9)
- Human-in-the-loop approval gates (Stage 18)
- LLM integration for the Commander (workers are tool-driven; LLM-based reasoning comes later)

## 3. Design decisions

### 3.1 Commander sits above Orchestrator

**Decision**: The `IncidentCommander` receives the `Diagnosis` from `OrchestratorAgent.run()` and all `WorkerFinding` objects, then produces a `CommanderDiagnosis`.

**Rationale**:
- The Commander adds a strategic layer that the Orchestrator does not provide
- The Commander does not dispatch workers — it receives their output
- This preserves the separation of concerns: Orchestrator coordinates, Commander adjudicates
- The hierarchical pattern mirrors real-world incident response structures

### 3.2 CommanderDiagnosis extends AgentOutput

**Decision**: `CommanderDiagnosis` inherits from `AgentOutput` and contains the full `Diagnosis` and all `WorkerFinding` objects as nested Pydantic models.

**Rationale**:
- Consistent with the Stage 4 cross-cutting rule (every agent output is a validated Pydantic object)
- Nested validated objects ensure the entire hierarchy is schema-compliant
- The CommanderDiagnosis can be serialized independently while preserving all underlying data
- Follows the OpenSpec agent responsibility matrix (Incident Commander reads all worker outputs, writes incident summary and escalation decision)

### 3.3 Escalation logic is deterministic

**Decision**: The escalation decision (`escalate`, `monitor`, `resolve`) is determined by a deterministic function based on risk tier, confidence, and worker coverage — not by LLM calls.

**Rationale**:
- Avoids LLM hallucination in critical escalation decisions
- Provides auditable, deterministic escalation logic
- Matches the OpenSpec risk classification rules
- Can be enhanced with LLM-based reasoning in later stages without changing the decision framework

### 3.4 No actions — narrative only

**Decision**: The Commander produces narratives and escalation decisions but does not execute remediation.

**Rationale**:
- The roadmap explicitly states Stage 7 output is "end-to-end diagnosis flow (no actions yet)"
- Actions require human-in-the-loop approval (Stage 18)
- This keeps the Commander focused on synthesis and judgment
- Remediation is handled by dedicated agents in Stage 16+

## 4. Schema definitions

### 4.1 CommanderDiagnosis

```python
class CommanderDiagnosis(AgentOutput):
    incident_id: str
    narrative: str          # Unified incident narrative
    escalation_decision: str  # "escalate", "monitor", or "resolve"
    severity_assessment: str  # Overall severity description
    confidence: float        # Commander confidence (0.0-1.0)
    risk_tier: RiskTier      # Inherited from diagnosis
    diagnosis: Diagnosis     # The underlying orchestrator diagnosis
    worker_findings: list[WorkerFinding]  # All worker findings
    evidence: list[EvidenceItem]          # Aggregated evidence
    affected_service: str
    category: str
    recommendation: str      # Commander-level recommendation
    reasoning_steps: list[ReasoningStep]  # Commander-level reasoning
```

### 4.2 IncidentCommander pipeline

```
Input: Diagnosis + WorkerFindings dict + incident_id
  → synthesize_narrative()
  → determine_escalation()
  → assess_severity()
  → build_evidence()
  → build_reasoning()
  → build_recommendation()
  → CommanderDiagnosis
Output: CommanderDiagnosis
```

## 5. Acceptance criteria

- [x] `CommanderDiagnosis` Pydantic model with narrative, escalation_decision, severity_assessment, diagnosis, worker_findings, evidence, reasoning_steps
- [x] `CommanderDiagnosis` inherits from `AgentOutput`
- [x] `CommanderDiagnosis` all fields validated by Pydantic
- [x] `IncidentCommander` class with `synthesize_narrative()`, `determine_escalation()`, `assess_severity()`, `run()`
- [x] `IncidentCommander.run()` produces a schema-valid `CommanderDiagnosis`
- [x] Escalation decision is "escalate" for high-risk incidents
- [x] Escalation decision is "monitor" for medium-risk with low confidence
- [x] Escalation decision is "resolve" for low-risk with good confidence
- [x] Severity assessment reflects risk tier and evidence count
- [x] Narrative combines diagnosis root cause with worker summaries
- [x] Commander's `run()` method executes full pipeline
- [x] `CommanderDiagnosis.to_json()` and `from_json()` work correctly
- [x] `CommanderDiagnosis.format_report()` produces human-readable report
- [x] Cross-cutting rule verified: CommanderDiagnosis is a validated Pydantic object
- [x] `CommanderDiagnosis` contains the full underlying `Diagnosis` object
- [x] `CommanderDiagnosis` contains all `WorkerFinding` objects
- [x] All tests pass (Stage 7 + existing stages)
- [x] Backward compatible: `ReActAgent` and `OrchestratorAgent` still work unchanged

## 6. Risks / open questions

- **Nested Pydantic models**: `CommanderDiagnosis` contains a `Diagnosis` and `list[WorkerFinding]` as nested Pydantic models. This requires careful serialization handling. The `model_dump()` method handles enum conversion, and Pydantic v2 handles nested validation natively.
- **Narrative quality**: The narrative is currently constructed from structured data (template-based), not generated by an LLM. This is intentional for Stage 7 — LLM-based narrative generation comes later.
- **Escalation determinism**: The escalation logic is deterministic and based on simple rules. This could be enhanced with ML-based escalation prediction in later stages, but the current approach is auditable and reliable.
- **Commander confidence**: The Commander's confidence is currently `min(diagnosis.confidence + 0.05, 0.95)` — a simple bump. More sophisticated confidence calculations could be added later.
