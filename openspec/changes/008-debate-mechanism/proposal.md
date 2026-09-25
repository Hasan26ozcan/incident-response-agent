# Proposal: Stage 8 — Debate Mechanism

- **Status:** Approved
- **Stage:** 8 of 23
- **Phase:** C — Multi-Agent Architecture
- **Depends on:** Stage 7 (Hierarchical Swarm)

## 1. Problem / Motivation

At Stage 7, the `IncidentCommander` synthesizes worker findings into a unified narrative and makes escalation decisions. However, the diagnosis is never challenged — there is no mechanism to question whether the initial root-cause analysis is correct or a false positive.

Stage 8 introduces the **debate mechanism**: a structured adversarial process where a **RootCauseAgent** argues in favor of the initial diagnosis, while a **ForensicExaminerAgent** challenges it by looking for false positives, alternative explanations, and weak evidence. The debate produces a `DebateOutcome` containing a before/after false-positive rate comparison and a final verdict.

This stage establishes the adversarial analysis pattern that improves diagnostic accuracy and provides auditable reasoning about why a diagnosis was confirmed or revised.

## 2. Scope

### In scope

- **`RootCauseAgent` class**:
  - `build_arguments(diagnosis)` — produces supporting arguments citing evidence
  - `analyze(diagnosis)` — entry point that returns list of `Argument` objects
- **`ForensicExaminerAgent` class**:
  - `build_challenges(diagnosis)` — produces challenges looking for false positives
  - `challenge(diagnosis)` — entry point that returns list of `Argument` objects
- **`DebateMechanism` class**:
  - `run(diagnosis)` — full debate pipeline producing `DebateOutcome`
  - `calculate_false_positive_rate()` — deterministic FPR calculation
  - `determine_verdict()` — verdict logic (confirmed/challenged/revised)
- **`Argument` Pydantic model**:
  - `agent`, `point`, `evidence_refs`, `confidence`
- **`DebateOutcome` Pydantic model**:
  - Extends `AgentOutput` with original_diagnosis, root_cause_arguments, forensic_challenges, verdict, false_positive_rate_before/after, confidence_adjustment, final_diagnosis, debate_rounds, reasoning_steps, recommendation
- **Cross-cutting rule maintained**: every agent output is a validated Pydantic object (Stage 4 requirement)
- **Test suite**: comprehensive tests for all debate components and false-positive rate comparison

### Out of scope

- Remediation actions (Stage 16+)
- Tree-of-Thought branching (Stage 9)
- Human-in-the-loop approval gates (Stage 18)
- LLM-based debate (debate logic is deterministic in Stage 8)

## 3. Design decisions

### 3.1 Debate is deterministic

**Decision**: The debate mechanism uses deterministic logic for argument generation, challenge identification, and verdict determination — no LLM calls.

**Rationale**:
- Avoids LLM hallucination in critical diagnostic validation
- Provides auditable, reproducible debate outcomes
- The false-positive rate comparison is a quantitative metric
- LLM-based debate could be added in later stages without changing the framework

### 3.2 False-positive rate is quantitative

**Decision**: The FPR is calculated deterministically from confidence, evidence count, and challenge count.

**Rationale**:
- Provides a measurable before/after comparison
- Does not require ground-truth labels (which may not be available in production)
- The formula is transparent and auditable
- Can be refined with actual accuracy data in later stages

### 3.3 Verdict drives confidence adjustment

**Decision**: The verdict ("confirmed", "challenged", "revised") directly adjusts the diagnosis confidence.

**Rationale**:
- "confirmed" increases confidence slightly (+0.05)
- "challenged" decreases confidence slightly (-0.05)
- "revised" decreases confidence more substantially (-0.10)
- This provides a clear, actionable signal downstream

### 3.4 Debate rounds allow iterative refinement

**Decision**: The debate proceeds for multiple rounds (default 2) where each side responds to the other's arguments.

**Rationale**:
- Allows the debate to surface deeper issues
- Each round adds more nuance to the analysis
- Prevents superficial one-shot debates
- The number of rounds is configurable

## 4. Schema definitions

### 4.1 Argument

```python
class Argument(BaseModel):
    agent: str                    # "root_cause" or "forensic_examiner"
    point: str                    # The argument point (min 5 chars)
    evidence_refs: list[str]      # Supporting evidence references
    confidence: float             # Confidence in this argument (0.0-1.0)
```

### 4.2 DebateOutcome

```python
class DebateOutcome(AgentOutput):
    incident_id: str
    original_diagnosis: Diagnosis
    root_cause_arguments: list[Argument]
    forensic_challenges: list[Argument]
    verdict: str                  # "confirmed", "challenged", or "revised"
    false_positive_rate_before: float
    false_positive_rate_after: float
    confidence_adjustment: float
    final_diagnosis: Diagnosis
    debate_rounds: int
    reasoning_steps: list[ReasoningStep]
    recommendation: str
```

### 4.3 Debate pipeline

```
Input: Diagnosis
  → RootCauseAgent.build_arguments()
  → ForensicExaminerAgent.build_challenges()
  → Debate rounds (exchange arguments)
  → calculate_false_positive_rate(before)
  → determine_verdict()
  → Adjust confidence → final_diagnosis
  → calculate_false_positive_rate(after)
  → DebateOutcome
Output: DebateOutcome
```

## 5. Acceptance criteria

- [x] `Argument` Pydantic model with agent, point, evidence_refs, confidence
- [x] `DebateOutcome` Pydantic model extending `AgentOutput`
- [x] `DebateOutcome` all fields validated by Pydantic
- [x] `RootCauseAgent` class with `build_arguments()` and `analyze()`
- [x] `ForensicExaminerAgent` class with `build_challenges()` and `challenge()`
- [x] `DebateMechanism` class with `run()`, `calculate_false_positive_rate()`, `determine_verdict()`
- [x] `DebateMechanism.run()` produces a schema-valid `DebateOutcome`
- [x] Debate produces before/after false-positive rate comparison
- [x] Verdict is "confirmed" when root-cause arguments outweigh challenges
- [x] Verdict is "challenged" when challenges are moderate
- [x] Verdict is "revised" when challenges outweigh root-cause arguments
- [x] `DebateOutcome.to_json()` and `from_json()` work correctly
- [x] `DebateOutcome.format_report()` produces human-readable report
- [x] Cross-cutting rule verified: `DebateOutcome` is a validated Pydantic object
- [x] `DebateOutcome` contains the original `Diagnosis` and `final_diagnosis`
- [x] All tests pass (Stage 8 + existing stages)
- [x] Backward compatible: `ReActAgent`, `OrchestratorAgent`, and `IncidentCommander` still work unchanged
- [x] `ruff check`, `ruff format`, `mypy`, `bandit` — all green

## 6. Risks / open questions

- **Debate quality**: The debate arguments are currently template-based, not LLM-generated. This is intentional for Stage 8 — LLM-based debate could be added in later stages.
- **FPR formula**: The false-positive rate formula is a heuristic. It may not perfectly reflect real-world FPR but provides a useful relative comparison.
- **Debate rounds**: The default of 2 rounds may be insufficient for complex incidents. The `debate_rounds` parameter is configurable.
- **Circular imports**: The debate module imports from both `root_cause_agent.py` and `forensic_examiner_agent.py`, which both import from `debate.py`. This is safe because `debate.py` only imports from `diagnosis.py` and `agent_output.py` (no circular dependency).
