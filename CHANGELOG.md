# Changelog

All notable changes to the Incident Response Agent project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Stage 1] — 2026-09-14
### Added
- 20 synthetic incident scenarios (`data/incidents/INC-001`…`INC-020`) covering 19 categories
- Each scenario has `meta.json`, `logs.log`, `metrics.json`, `deploys.json`
- 20 gold answers (`eval/gold/INC-001.json`…`INC-020.json`) with root cause, evidence, remediation, and risk tier
- `eval/rubric.md` — composite scoring rubric with two hard gates (false-positive handling, safety/risk-tier compliance)
- `eval/gold_set.md` — mechanically generated human-readable summary table
- `data/generate_dataset.py` — deterministic, stdlib-only dataset generator
- `eval/validate_dataset.py` — mechanical integrity/leakage validator
- `eval/generate_gold_set_md.py` — regenerates `gold_set.md` from gold JSON files
- Full test suite (`tests/`) with 187 tests covering generator, validator, integrity, leakage, false-alarm, recurrence, and distractor invariants
- `requirements.txt` — project dependencies (torch, numpy)
- `tests/test_torch_env.py` — verifies torch/numpy environment
- `tests/test_dataset.py` — comprehensive dataset tests
- `.venv/` Python environment with PyTorch 2.14.0+cpu and NumPy 2.4.6
### Changed
- `.gitignore` — fixed merge conflict markers, added eval/data artifact paths
### Verified
- All 187 tests pass (`python -m pytest tests/ -v`)
- `data/generate_dataset.py` runs cleanly and is deterministic across runs
- `eval/validate_dataset.py` passes with no problems found
- PyTorch 2.14.0+cpu and NumPy 2.4.6 available and interoperable
- Gold answers kept structurally separate from agent-visible data (no eval leakage)

## [Stage 9] — 2026-09-25
### Added
- `src/incident_agent/schemas/tree_of_thought.py` — `Hypothesis` and `TreeOfThoughtResult` Pydantic models:
  - `Hypothesis` model with `hypothesis_id`, `scenario`, `root_cause`, `confidence`, `supporting_evidence`, `reasoning_steps`, `category`, `risk_tier`
  - `TreeOfThoughtResult` extends `AgentOutput` with `hypotheses`, `selected_hypothesis`, `selected_hypothesis_index`, `reasoning_steps`, `selected_evidence`, `plan_and_solve_validation`, `accuracy_improvement`, `final_diagnosis`, `hypothesis_scores`
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
  - Pydantic validators for `accuracy_improvement`, `confidence`, and `verdict`
- `src/incident_agent/agents/tree_of_thought_agent.py` — `TreeOfThoughtAgent` class:
  - `generate_hypotheses(diagnosis)` — produces N parallel alternative root-cause hypotheses
  - `evaluate_hypotheses(hypotheses)` — scores each hypothesis against evidence
  - `select_best_hypothesis(hypotheses, scores)` — picks the highest-scoring hypothesis
  - `plan_and_solve(hypothesis, diagnosis)` — validates selected hypothesis via Plan-and-Solve
  - `run(diagnosis)` — full Tree-of-Thought pipeline producing `TreeOfThoughtResult`
- `tests/test_stage9.py` — 58 tests covering Hypothesis schema, TreeOfThoughtResult schema, hypothesis generation and scoring, best-hypothesis selection, Plan-and-Solve validation, accuracy improvement, schema validation, and backward compatibility
- `src/incident_agent/schemas/__init__.py` — Added `Hypothesis`, `TreeOfThoughtResult` exports
- `src/incident_agent/agents/__init__.py` — Added `TreeOfThoughtAgent` export
- `src/incident_agent/__init__.py` — Added `Hypothesis`, `TreeOfThoughtResult`, `TreeOfThoughtAgent` exports

### Changed
- `src/incident_agent/schemas/__init__.py` — Added `Hypothesis` and `TreeOfThoughtResult` exports
- `src/incident_agent/agents/__init__.py` — Added `TreeOfThoughtAgent` export
- `src/incident_agent/__init__.py` — Added `Hypothesis`, `TreeOfThoughtResult`, `TreeOfThoughtAgent` exports

### Verified
- All 519 tests pass (58 Stage 9 + 461 existing)
- `TreeOfThoughtAgent.run()` produces a schema-valid `TreeOfThoughtResult`
- Multiple hypotheses (≥2) are generated in parallel
- Best hypothesis is selected based on highest evaluation score
- Plan-and-Solve validation is performed on the selected hypothesis
- Accuracy improvement is measurable (0.0–1.0 range)
- Cross-cutting rule verified: `TreeOfThoughtResult` is a validated Pydantic object
- Backward compatible: `ReActAgent`, `OrchestratorAgent`, `IncidentCommander`, and `DebateMechanism` still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 8] — 2026-09-23
### Added
- `src/incident_agent/schemas/debate.py` — `Argument` and `DebateOutcome` Pydantic models:
  - `Argument` model with `agent`, `point`, `evidence_refs`, `confidence`
  - `DebateOutcome` extends `AgentOutput` with `original_diagnosis`, `root_cause_arguments`, `forensic_challenges`, `verdict`, `false_positive_rate_before/after`, `confidence_adjustment`, `final_diagnosis`, `debate_rounds`, `reasoning_steps`, `recommendation`
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
  - Pydantic validators for `verdict` and confidence bounds
- `src/incident_agent/agents/root_cause_agent.py` — `RootCauseAgent` class:
  - `build_arguments(diagnosis)` — produces supporting arguments citing evidence
  - `analyze(diagnosis)` — entry point returning list of `Argument` objects
- `src/incident_agent/agents/forensic_examiner_agent.py` — `ForensicExaminerAgent` class:
  - `build_challenges(diagnosis)` — produces challenges looking for false positives
  - `challenge(diagnosis)` — entry point returning list of `Argument` objects
- `src/incident_agent/agents/debate_mechanism.py` — `DebateMechanism` class:
  - `run(diagnosis)` — full debate pipeline producing `DebateOutcome`
  - `calculate_false_positive_rate()` — deterministic FPR calculation
  - `determine_verdict()` — verdict logic (confirmed/challenged/revised)
- `openspec/changes/008-debate-mechanism/proposal.md` — OpenSpec proposal
- `tests/test_stage8.py` — 55 tests covering Argument schema, DebateOutcome schema, RootCauseAgent, ForensicExaminerAgent, DebateMechanism pipeline, false-positive rate comparison, verdict determination, debate rounds, schema validation, and backward compatibility
- `src/incident_agent/schemas/__init__.py` — Added `Argument`, `DebateOutcome` exports
- `src/incident_agent/agents/__init__.py` — Added `RootCauseAgent`, `ForensicExaminerAgent`, `DebateMechanism` exports
- `src/incident_agent/__init__.py` — Added `Argument`, `DebateOutcome`, all new agents

### Verified
- All 461 tests pass (55 Stage 8 + 406 existing)
- `DebateMechanism.run()` produces a schema-valid `DebateOutcome`
- False-positive rate comparison shows before/after FPR values
- Verdict is "confirmed" when root-cause arguments outweigh challenges
- Verdict is "challenged" when challenges are moderate
- Verdict is "revised" when challenges outweigh root-cause arguments
- Cross-cutting rule verified: `DebateOutcome` is a validated Pydantic object
- Backward compatible: `ReActAgent`, `OrchestratorAgent`, and `IncidentCommander` still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 7] — 2026-09-22
### Added
- `src/incident_agent/agents/incident_commander.py` — Incident Commander agent:
  - `IncidentCommander` class sits above OrchestratorAgent in the hierarchy
  - `synthesize_narrative()` — builds unified incident narrative from diagnosis and worker findings
  - `determine_escalation()` — makes escalation decision (escalate / monitor / resolve)
  - `assess_severity()` — evaluates overall severity from risk tier and evidence
  - `build_reasoning()` — builds commander-level reasoning steps
  - `build_evidence()` — aggregates evidence from diagnosis and all workers
  - `build_recommendation()` — produces commander-level recommendation
  - `run()` — full commander pipeline producing CommanderDiagnosis
- `src/incident_agent/schemas/command_diagnosis.py` — CommanderDiagnosis Pydantic model:
  - Extends AgentOutput with narrative, escalation_decision, severity_assessment
  - Contains the full underlying Diagnosis and all WorkerFinding objects
  - All fields validated by Pydantic (cross-cutting rule)
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
- `src/incident_agent/schemas/__init__.py` — Exports CommanderDiagnosis
- `src/incident_agent/agents/__init__.py` — Exports IncidentCommander
- `src/incident_agent/__init__.py` — Exports IncidentCommander and CommanderDiagnosis
- `openspec/changes/007-hierarchical-swarm/proposal.md` — OpenSpec proposal
- `tests/test_stage7.py` — 45+ tests covering CommanderDiagnosis schema, escalation logic, severity assessment, narrative synthesis, reasoning, evidence, recommendation, end-to-end pipeline, and backward compatibility

### Changed
- `src/incident_agent/schemas/__init__.py` — Added CommanderDiagnosis export
- `src/incident_agent/agents/__init__.py` — Added IncidentCommander export
- `src/incident_agent/__init__.py` — Added IncidentCommander and CommanderDiagnosis exports

### Verified
- All 401+ tests pass (45+ Stage 7 + 356 existing)
- IncidentCommander.run() produces a schema-valid CommanderDiagnosis
- CommanderDiagnosis contains the full underlying Diagnosis object
- CommanderDiagnosis contains all WorkerFinding objects
- Escalation decisions are deterministic based on risk tier and confidence
- Severity assessment reflects risk tier and evidence count
- Narrative synthesis combines diagnosis root cause with worker summaries
- CommanderDiagnosis.to_json() → from_json() roundtrip works correctly
- Cross-cutting rule verified: CommanderDiagnosis is a validated Pydantic object
- Backward compatible: ReActAgent and OrchestratorAgent still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 6] — 2026-09-21
### Added
- `src/incident_agent/agents/worker.py` — Specialist worker agents:
  - `WorkerAgent` base class (ABC) with `observe()` / `analyze()` / `run()` pipeline
  - `LogWorker` — parses logs, extracts error patterns, produces `WorkerFinding`
  - `MetricsWorker` — analyzes metric time series, detects anomalies, produces `WorkerFinding`
  - `DeployHistoryWorker` — correlates deploys with incident window, produces `WorkerFinding`
- `src/incident_agent/agents/orchestrator.py` — OrchestratorAgent (triage agent):
  - `OrchestrationState` — lightweight state machine (`INITIALIZED` → `DISPATCHED` → `PROCESSING` → `COMPLETED` → `SYNTHESIZED`)
  - `OrchestrationStatus` — tracks completed/failed workers and findings
  - `OrchestratorAgent.classify_incident()` — reads metadata and classifies risk
  - `OrchestratorAgent.dispatch_workers()` — creates and runs all three specialist workers
  - `OrchestratorAgent.synthesize()` — combines worker findings into a unified `Diagnosis`
  - `OrchestratorAgent.run()` — full orchestration pipeline
- `src/incident_agent/schemas/agent_output.py` — New `WorkerFinding` Pydantic model:
  - `worker_type`, `incident_id`, `evidence`, `reasoning_steps`, `confidence`, `summary`
  - All fields validated by Pydantic (cross-cutting rule)
- `src/incident_agent/schemas/__init__.py` — Exports `WorkerFinding` and `EvidenceType`
- `src/incident_agent/agents/__init__.py` — Exports all worker and orchestrator classes
- `src/incident_agent/__init__.py` — Exports all Stage 6 additions
- `openspec/changes/006-orchestrator-workers/proposal.md` — OpenSpec proposal
- `tests/test_stage6.py` — 52 tests covering orchestration state machine, all three workers, orchestrator dispatch/synthesize/run, backward compatibility, and schema validation

### Changed
- `src/incident_agent/agents/react_agent.py` — `run()` still uses plan-based execution with replanning (Stage 5); now available alongside `OrchestratorAgent`
- `src/incident_agent/__init__.py` — Added `OrchestratorAgent`, `OrchestrationState`, `LogWorker`, `MetricsWorker`, `DeployHistoryWorker`, `WorkerAgent`, `WorkerFinding`, `EvidenceType`

### Verified
- All 356 tests pass (52 Stage 6 + 304 existing)
- Orchestrator dispatches all three workers and synthesizes a valid `Diagnosis`
- Each worker produces a schema-valid `WorkerFinding`
- `OrchestrationState` tracks all state transitions correctly
- Workers handle failures gracefully without crashing the orchestration
- Backward compatible: `ReActAgent` still produces `single_agent_react` diagnoses
- Cross-cutting rule verified: every agent output is a validated Pydantic object
- ruff check, ruff format, mypy, bandit — all green

## [Stage 5] — 2026-09-19
### Added
- `src/incident_agent/workflows/plan.py` — Plan, PlanStep, PlanStepType, StepFailure, generate_plan()
  - `Plan` dataclass with ordered `PlanStep` sequence, replan_count tracking
  - `PlanStep` frozen dataclass with step_type, description, order
  - `StepFailure` exception carrying the failed step and reason
  - `generate_plan(incident_id, strategy)` supporting "standard", "log_only", "metrics_only"
- `src/incident_agent/agents/react_agent.py` — Dynamic planning and error recovery (Stage 5)
  - `ReActAgent.generate_plan()` — produces a diagnostic plan before execution
  - `ReActAgent.execute_plan()` — runs all plan steps, raising StepFailure on invalid results
  - `ReActAgent.replan()` — rebuilds the plan from failure feedback (skip failed step, substitute alternative strategy)
  - `ReActAgent.run()` — updated to execute plan with replanning loop (max_replans=2 default)
  - Cross-cutting rule maintained: every output is still a schema-validated Pydantic Diagnosis
- `src/incident_agent/agents/__init__.py` — exports Plan, StepFailure
- `src/incident_agent/__init__.py` — exports Plan, StepFailure
- `src/incident_agent/workflows/__init__.py` — exports Plan, PlanStep, StepFailure, generate_plan
- `tests/test_stage5.py` — 35 tests covering plan generation, step execution, replanning, error recovery, schema validation

### Changed
- `src/incident_agent/agents/react_agent.py` — `run()` now uses plan-based execution with replanning instead of a single observe/reason/act pass

### Verified
- All 304 tests pass (269 existing + 35 Stage 5)
- Replanning recovers from injected StepFailure and produces schema-valid Diagnosis objects
- Plan generation works for all three strategies (standard, log_only, metrics_only)
- Backward compatible: all Stage 3 and Stage 4 tests still pass unchanged

## [Stage 4] — 2026-09-18
### Added
- `src/incident_agent/schemas/` package with Pydantic models:
  - `Diagnosis` — replaces `dataclasses.Diagnosis` with full schema validation
  - `EvidenceItem`, `ReasoningStep`, `IncidentMetadata`, `MetricAnomaly`, `AgentOutput`, `RiskTier`
- `src/incident_agent/prompts/` package with prompt library:
  - `system_prompt.py` — system prompt template with role, constraints, and output schema
  - `few_shot.py` — three curated few-shot examples covering diverse categories and risk tiers
  - `builder.py` — `build_prompt()` utility to assemble complete prompts
- `ReActAgent` updated to produce Pydantic-validated `Diagnosis` objects
- Cross-cutting rule enforced: every agent output is a validated Pydantic object
- `tests/test_stage4.py` — 49 schema validation and prompt library tests
- OpenSpec proposal `openspec/changes/004-structured-output-prompt-engineering/proposal.md`
- Pydantic added to `pyproject.toml` and `requirements.txt`
### Changed
- `src/incident_agent/agents/react_agent.py` — now produces Pydantic `Diagnosis` instead of `dataclasses.Diagnosis`
- `src/incident_agent/__init__.py` — exports all Pydantic schema types
- `src/incident_agent/agents/__init__.py` — exports Pydantic `Diagnosis`
- `src/incident_agent/schemas/__init__.py` — new canonical schema exports
### Verified
- All 82 tests pass (49 Stage 4 + 28 Stage 3 + 5 smoke)
- Schema validation catches invalid inputs (missing fields, out-of-bounds confidence, empty evidence)
- JSON round-trip (`to_json` → `from_json`) produces valid Diagnosis objects
- Prompt builder produces valid structured prompts with few-shot examples
- Backward compatible: existing `format_report()` and CLI work unchanged

## [Stage 3] — 2026-09-16
### Added
- `src/incident_agent/agents/react_agent.py` — ReActAgent with observe/reason/act/repeat loop
- `src/incident_agent/tools/` — tool functions: read_meta, read_logs, find_error_logs, detect_anomaly_timestamps, get_recent_deploys, find_metric_spikes, read_metrics, read_deploys
- `src/incident_agent/cli.py` — diagnose subcommand
- `Diagnosis` dataclass with structured output and format_report()
- 28 comprehensive Stage 3 tests
### Changed
- README.md — updated Stage 3 status
### Verified
- All 220 tests passing
- ruff, mypy, bandit clean

## [Stage 2] — 2026-09-15
### Added
- Project skeleton: `src/incident_agent/` package with subpackages (`agents/`, `tools/`, `memory/`, `workflows/`, `eval/`)
- `pyproject.toml` with hatchling build, ruff/bandit/mypy config
- CI pipeline (`.github/workflows/ci.yml`): ruff, bandit, mypy, pytest, trivy, dataset regeneration check
- `Makefile` with 10 targets (`make ci`, `make sandbox-build`, `make sandbox-shell`, etc.)
- `.pre-commit-config.yaml` (ruff, pre-commit hooks)
- Docker configuration: `Dockerfile`, `docker-compose.yml`, `docker/sandbox/Dockerfile` (hardened non-root image)
- `docker/sandbox/README.md` — sandbox invocation patterns and rationale
- `src/incident_agent/cli.py` — console script entry point placeholder
- `src/incident_agent/py.typed` — marker file for type-aware imports
- 5 Stage-2 smoke tests (`tests/test_smoke.py`) — package install, CLI, subpackage imports
- **192 tests total, all passing**
### Changed
- `.gitignore` — added data/generated/, eval/results/ paths
- README.md — updated Stage 2 status, corrected test count to 192
### Verified
- All 192 tests pass
- ruff check, ruff format, mypy, bandit — all green
- Docker image builds and runs as non-root user
- Sandbox has no network access by default

## [Stage 0] — 2026-09-13
### Added
- `openspec/proposal.md` — Scope, architecture overview, design decisions, acceptance criteria
- `openspec/agent-responsibility-matrix.md` — Per-agent responsibility, read/write scope, risk ceiling
- `openspec/risk-classification.md` — Low / medium / high risk taxonomy with governance rules
- `openspec/comparison-bmad-speckit-openspec.md` — Methodology comparison and rationale for choosing OpenSpec
- `openspec/README.md` — OpenSpec workflow documentation
- `CHANGELOG.md` — This file

---

## Template for future stages

Each stage adds its own section here following this pattern:

```markdown
## [Stage N] — <Title> — <YYYY-MM-DD>
### Added
- What was delivered
### Changed
- What was modified from prior stages
### Verified
- Acceptance criteria met
```
