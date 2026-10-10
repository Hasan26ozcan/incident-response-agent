# Proposal: Stage 6 (rewrite) — LLM Orchestrator–Workers

- **Status:** Implemented, awaiting measurement
- **Stage:** 6 of 23 (supersedes the rule-based implementation of change 006 for evaluation purposes)
- **Depends on:** Stage 5 (LLMReActAgent, guarded tool loop), Stage 2 (eval harness)

## 1. Why a rewrite

The original Stage 6 (`agents/orchestrator.py`, `agents/worker.py`) is kept for backward compatibility
(Stages 7+ import it) but it is not a real diagnostic system. Measured on all 20 gold incidents with the
current harness:

| metric | legacy orchestrator |
|---|---|
| root_cause_f1 | 0.068 |
| answered_rate | 0.15 |
| risk_accuracy | 0.00 |
| crashed | 1 / 20 |

Causes: it reads `meta["category"]`, which no `meta.json` contains (the dataset is intentionally
non-spoiler), so classification is always "unknown"; and `_infer_root_cause` / `_build_recommendation`
match hard-coded phrases ("unbounded retry loop in the payment-confirmation handler"). Workers produce
no reasoning of their own. This is pattern matching on one scenario, not diagnosis.

## 2. Design

```
alert metadata ─► Triage lead (1 LLM call) ─► hypotheses + focus per worker
                          │
        ┌─────────────────┼─────────────────┐   (threads, independent failure)
     log worker      metrics worker     deploy worker
   log_overview      get_metric_summary  get_recent_deploys
   search_logs
        │  each: guarded ToolLoop ─► WorkerReport (schema-validated, quotes verified)
        └─────────────────┬─────────────────┘
                 Synthesis (1 LLM call) ─► LLMDiagnosis ─► shared grounding check ─► Diagnosis
```

Decisions:
- **Triage cannot drop workers.** It only supplies hypotheses and focus questions. This keeps coverage
  fixed so the ablation (§4) measures the architecture, not a skipped data source.
- **Tool scoping is enforced twice**: the spec list shown to the model and `IncidentToolbox(allowed_tools=…)`.
- **Failure isolation.** A worker that raises (outage, invalid output, bug) is recorded; the others
  continue and synthesis notes the missing coverage. All workers failing, or a failed synthesis,
  yields the same flagged zero-confidence `degraded` diagnosis as the single agent. `LLMConfigError`
  (bad key/model) is never swallowed.
- **Shared infrastructure** with the single agent: `ToolLoop` (duplicate/budget/malformed-call guards,
  `<tool_output>` wrapping) and `ground_diagnosis` (evidence tokens vs. tool output, −0.15 confidence
  per ungrounded item). Same scoring path, same accounting (`TrackingClient` is thread-safe).
- **Parallelism** via threads (LLM calls are I/O bound). `parallel=False` gives a deterministic order.
- Legacy classes and LangGraph are out of scope; the state is plain Python.

## 3. Acceptance criteria

- [x] Triage, three scoped workers, synthesis; every output a validated Pydantic object
- [x] Worker failure, outage, invalid output, triage failure: none crash the run (tests)
- [x] Worker tool scope enforced by the toolbox
- [x] Harness adapter `--agent llm_orchestrator` with per-worker trace and usage
- [x] Single-agent behaviour unchanged by the refactor (all earlier tests green)
- [ ] **Measured**: dev-split ablation below, recorded in `eval/results/`

## 4. Ablation protocol (dev split only; test split opened once, at the milestone)

Run each with the same model and `--temperature 0`, one `--tag` each:

1. `llm_react` (single agent)  — baseline
2. `llm_orchestrator` (full)
3. `llm_orchestrator` without LLM triage (`use_llm_triage=False`)

Compare root_cause_f1, evidence_recall, risk_accuracy, false_alarm_accuracy, crashed/degraded,
llm_calls and tokens. Orchestration costs roughly 3x the calls; it must earn that on accuracy or the
honest conclusion is "single agent is enough for this dataset size", which is a valid result.
Per-incident differences go in the write-up, not only means (10 dev incidents is small).

## 5. Risks

- Free-tier rate limits: ~15–20 calls per incident, concurrent workers. Mitigation: `parallel=False`,
  response cache, fallback provider.
- Worker reports can launder a wrong guess into synthesis; mitigated by quote verification
  (`verified` flag per fact) but not eliminated.
