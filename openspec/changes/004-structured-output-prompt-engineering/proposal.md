# Proposal: Stage 4 — Structured Output & Prompt Engineering

- **Status:** Approved
- **Stage:** 4 of 23
- **Phase:** B — Agent Fundamentals
- **Depends on:** Stage 3 (Single-Agent ReAct skeleton)

## 1. Problem / Motivation

At Stage 3, the ReAct agent produces a `dataclasses.Diagnosis` object — but there is no enforcement that outputs conform to a schema. Downstream consumers (multi-agent orchestration, eval harnesses, prompt inputs) receive free-text or loosely-typed data. This creates:

- **Type ambiguity**: agents cannot reliably parse each other's outputs
- **Parsing fragility**: free-text responses require fragile string parsing
- **No JSON-mode guarantee**: LLM calls produce unstructured text that must be parsed

Stage 4 solves this by mandating that **every agent output is a validated Pydantic object**, establishing disciplined prompt engineering with few-shot examples, and ensuring all LLM interactions use JSON mode or grammars.

This is a hard requirement per the cross-cutting rule in
`openspec/agent-responsibility-matrix.md` (rule #3):
> "Every agent's output is a validated Pydantic object (Stage 4 onward)."

## 2. Scope

### In scope

- **Pydantic schemas** for all agent output types:
  - `Diagnosis` — the primary diagnosis output (replaces `dataclasses.Diagnosis`)
  - `EvidenceItem` — individual evidence entries
  - `ReasoningStep` — steps in the reasoning chain
  - `IncidentMetadata` — parsed incident metadata
  - `MetricAnomaly` — detected metric anomalies
  - `AgentOutput` — base class for all agent outputs
  - `RiskTier` — risk classification enum
- **Prompt library**:
  - System prompt template with role, constraints, and output schema
  - Three curated few-shot examples covering diverse categories
  - `build_prompt()` utility to assemble complete prompts
- **ReActAgent update**: produce `Diagnosis` as a Pydantic object
- **Schema validation test suite**: comprehensive tests for all schemas and prompt library

### Out of scope

- Multi-agent orchestration (Stage 6+)
- Vector DB / retrieval (Stage 10+)
- MCP servers (Stage 13+)
- Temporal workflows (Stage 17+)

## 3. Design decisions

### 3.1 Pydantic over dataclasses

**Decision**: Replace `dataclasses.Diagnosis` with a Pydantic `BaseModel`.

**Rationale**:
- Pydantic provides runtime type validation, not just type hints
- `model_validate_json()` enables JSON-mode LLM grammar validation
- `model_dump()` produces deterministic dict/JSON output
- Pydantic v2 has excellent performance and a clean API
- Industry standard for structured AI agent output

### 3.2 Prompt library structure

**Decision**: Three modules — `system_prompt.py`, `few_shot.py`, `builder.py`.

**Rationale**:
- Separation of concerns: template, examples, assembly
- `build_prompt()` enables parameterized prompts for different incidents
- Few-shot examples are pre-built `Diagnosis` objects that serialize to JSON

### 3.3 JSON mode / grammars

**Decision**: All system prompts include instructions for JSON mode.

**Rationale**:
- JSON mode/grammars ensure LLM output is structurally valid
- Reduces parsing errors and hallucinated structure
- `Diagnosis.to_json()` and `Diagnosis.from_json()` provide round-trip serialization

## 4. Schema definitions

### 4.1 AgentOutput (base)

```python
class AgentOutput(BaseModel):
    schema_version: str = "1.0"
    produced_at: datetime
    agent_type: str
    incident_id: str  # pattern: INC-XXX
    confidence: float  # [0.0, 1.0]
```

### 4.2 Diagnosis

```python
class Diagnosis(AgentOutput):
    incident_id: str
    root_cause: str  # min 10 chars
    confidence: float  # [0.0, 1.0]
    evidence: list[EvidenceItem]  # min 1 item
    affected_service: str
    category: str
    reasoning_steps: list[ReasoningStep]  # min 1 step
    recommendation: str  # min 10 chars
    risk_tier: RiskTier
```

### 4.3 EvidenceItem

```python
class EvidenceItem(BaseModel):
    source_type: EvidenceType  # log_entry | metric_anomaly | deploy | meta
    source: str
    detail: str
    timestamp: str
    confidence_weight: float  # [0.0, 1.0]
```

## 5. Acceptance criteria

- [x] All agent output types are Pydantic models
- [x] `Diagnosis` replaces `dataclasses.Diagnosis` with full schema validation
- [x] `ReActAgent.act()` returns a `Diagnosis` Pydantic object
- [x] `Diagnosis.to_json()` produces valid JSON for JSON-mode LLM calls
- [x] `Diagnosis.from_json()` parses and validates JSON input
- [x] System prompt template includes role, constraints, and output schema
- [x] Three few-shot examples covering diverse categories and risk tiers
- [x] `build_prompt()` assembles complete prompts from template + examples + context
- [x] Schema validation test suite passes (49 tests)
- [x] All existing Stage 3 tests still pass (backward compatibility)
- [x] Cross-cutting rule #3 verified: every agent output is a validated Pydantic object

## 6. Risks / open questions

- **Confidence score accuracy**: The confidence calculation (`0.5 + evidence_count * 0.1`) is heuristic. Later stages may use LLM-based confidence scoring.
- **Category inference**: `meta.json` does not include `category` (intentionally non-spoiler). Category is inferred from logs in later stages.
- **JSON-mode LLM support**: The prompt library assumes JSON-mode LLM support. If the LLM does not support it, `from_json()` will fail. This is deferred to Stage 6 (multi-agent) when actual LLM calls are made.
- **Performance**: Pydantic validation adds minimal overhead (~0.1ms per object). Not a concern for this scale.
