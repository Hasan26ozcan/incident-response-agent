# Proposal: Stage 5 — Dynamic Planning & Error Recovery

- **Status:** Approved
- **Stage:** 5 of 23
- **Phase:** B — Agent Fundamentals
- **Depends on:** Stage 4 (Structured Output & Prompt Engineering)

## 1. Problem / Motivation

At Stage 4, the ReAct agent executes a fixed observe → reason → act loop. While the output is schema-validated, the agent has no awareness of *how* it plans to arrive at a diagnosis — it reads data and immediately produces a conclusion. This creates two problems:

- **No recovery mechanism**: If a data source is unavailable or returns unexpected results (e.g., empty error logs for a false-alarm incident), the agent has no strategy to adapt. It produces a diagnosis with thin evidence or fails entirely.
- **No explicit planning**: The order and scope of diagnostic steps are implicit. A more capable agent should be able to generate a diagnostic plan upfront and adjust it when a step fails — rebuilding from feedback rather than crashing.

Stage 5 solves this by introducing a **planning layer** to the ReAct agent. The agent generates a diagnostic plan before execution, executes it step-by-step, and when a step fails it replans — replacing the failed step with an alternative strategy. This makes the agent resilient to partial data unavailability and unexpected conditions.

This is the foundation for more sophisticated multi-agent orchestration in Stage 6+, where individual specialist agents will need their own planning and recovery capabilities.

## 2. Scope

### In scope

- **Plan data structures**:
  - `Plan` — a sequence of ordered diagnostic steps
  - `PlanStep` — a single step with type, description, and order
  - `PlanStepType` — enum of step types (READ_META, READ_LOGS, FIND_ERROR_LOGS, DETECT_ANOMALIES, READ_DEPLOYS, SYNTHESIZE)
  - `StepFailure` — exception raised when a step cannot produce valid results
- **Plan generation**:
  - `generate_plan(incident_id, strategy)` supporting "standard", "log_only", "metrics_only" strategies
- **ReActAgent plan integration**:
  - `ReActAgent.generate_plan()` — produces a diagnostic plan before execution
  - `ReActAgent.execute_plan()` — runs all plan steps, raising `StepFailure` on invalid results
  - `ReActAgent.replan()` — rebuilds the plan from failure feedback (skip failed step, substitute alternative strategy)
  - `ReActAgent.run()` — updated to execute plan with replanning loop (max_replans=2 default)
- **Cross-cutting rule maintained**: every agent output remains a validated Pydantic `Diagnosis` object (Stage 4 requirement)
- **Test suite**: comprehensive tests for plan generation, execution, replanning, and error recovery

### Out of scope

- Multi-agent orchestration (Stage 6+)
- Temporal workflows (Stage 17+)
- LLM integration (the plan system is currently tool-driven; LLM-based planning comes later)

## 3. Design decisions

### 3.1 Plan as a first-class data structure

**Decision**: `Plan` is a dataclass with an ordered list of `PlanStep` objects.

**Rationale**:
- Plans are inspectable, serializable, and replanable
- Steps are ordered so execution is deterministic
- Frozen `PlanStep` prevents accidental mutation during execution
- `PlanStepType` enum provides a clear contract for what each step does

### 3.2 StepFailure as an exception

**Decision**: `StepFailure` is an exception carrying the failed step and the reason.

**Rationale**:
- Allows `execute_plan()` to cleanly signal failure without returning error codes
- Carries enough context for `replan()` to decide the alternative strategy
- Follows the Python convention of "ask for forgiveness, not permission"

### 3.3 Replanning strategy

**Decision**: When a step fails, `replan()` removes the failed step and substitutes an alternative approach based on the failure type:
- `find_error_logs` failed → switch to metrics-based strategy (anomaly detection)
- `detect_anomalies` failed → switch to log-based strategy (error log scanning)
- Other failures → generate a reduced plan skipping the failed step type

**Rationale**:
- The most common failure modes in the synthetic dataset are:
  1. False alarms with no ERROR logs (INC-019, INC-020)
  2. Anomalies that don't trigger the detection threshold
- These two complementary strategies cover most real-world failure scenarios
- The replanning count is bounded (`max_replans`) to prevent infinite loops

### 3.4 Integration with existing ReAct loop

**Decision**: Plan execution replaces the monolithic `observe()` call but the `reason()` and `act()` phases remain unchanged.

**Rationale**:
- `observe()` becomes a plan-generation step rather than the sole data-gathering mechanism
- `reason()` and `act()` continue to produce the same validated output
- Backward compatibility: `ReActAgent.run("INC-001")` produces the same result as before, just with a planning layer added internally

## 4. Schema definitions

### 4.1 PlanStepType

```python
class PlanStepType(str, Enum):
    READ_META = "read_meta"
    READ_LOGS = "read_logs"
    FIND_ERROR_LOGS = "find_error_logs"
    DETECT_ANOMALIES = "detect_anomalies"
    READ_DEPLOYS = "read_deploys"
    SYNTHESIZE = "synthesize"
```

### 4.2 PlanStep

```python
@dataclass(frozen=True)
class PlanStep:
    step_type: PlanStepType
    description: str
    order: int
```

### 4.3 Plan

```python
@dataclass
class Plan:
    incident_id: str
    steps: list[PlanStep] = field(default_factory=list)
    replan_count: int = 0
    failed_step: str = ""
```

### 4.4 StepFailure

```python
class StepFailure(Exception):
    def __init__(self, step: PlanStep, reason: str) -> None:
        self.step = step
        self.reason = reason
```

## 5. Acceptance criteria

- [x] `Plan` dataclass with ordered steps, replan_count, failed_step
- [x] `PlanStep` frozen dataclass with step_type, description, order
- [x] `PlanStepType` enum covering all diagnostic step types
- [x] `StepFailure` exception carrying step and reason
- [x] `generate_plan(incident_id, strategy)` produces valid plans for all strategies
- [x] `ReActAgent.generate_plan()` returns a Plan
- [x] `ReActAgent.execute_plan()` runs steps and raises StepFailure on invalid results
- [x] `ReActAgent.replan()` rebuilds the plan from failure feedback
- [x] `ReActAgent.run()` uses plan-based execution with replanning loop
- [x] Replanning recovers from injected StepFailure (test showing recovery from a deliberately injected failure)
- [x] Recovered diagnoses are still schema-valid Pydantic objects
- [x] `max_replans` parameter bounds the replanning attempts
- [x] All 304 tests pass (35 Stage 5 + 269 existing)
- [x] Cross-cutting rule verified: every agent output is a validated Pydantic `Diagnosis`
- [x] Backward compatible: all Stage 3 and Stage 4 tests still pass

## 6. Risks / open questions

- **Replanning depth**: The current heuristic replanning (swap log↔metrics) is simple. For more complex failures, a graph-based replanning approach may be needed. This is deferred to Stage 6.
- **Step execution overhead**: `execute_plan()` calls tools directly. When LLM integration arrives (later stages), step execution will need to support tool-calling semantics.
- **max_replans default**: Set to 2 based on the observation that most failures are recoverable within 2 replans. This may need tuning with real incident data.
- **Plan serialization**: `Plan` is a dataclass, not a Pydantic model. If persistent plan storage is needed (Stage 12 memory), it should be wrapped in a Pydantic model.
