# 015 — LLM debate (Stage 8) and dataset v2

- **Status:** Implemented, unmeasured
- **Stage:** 8 of 23
- **Supersedes:** the deterministic debate in `changes/008-debate-mechanism` for the LLM line (legacy code is kept, not trusted)

## Why
The legacy Stage 8 had no model: its "false-positive rate" was a formula of confidence, evidence count and
challenge count, so it could not measure anything. The roadmap output ("before/after false-positive rate
comparison") needs real adversarial reasoning and a dataset that can expose both error directions. The gold set
had exactly one false alarm (INC-019, dev) and none in test.

## What
### `agents/llm_debate.py`
1. Defender (no tools): structured case for the diagnosis, verbatim quotes.
2. Examiner (read-only tools, budgeted): looks for NEW disconfirming evidence, then writes a structured case.
   An empty challenge list is a valid result.
3. Code gate: a challenge survives only if its quote is found in the evidence or the examiner's own tool output.
4. Rebuttal (no tools): each surviving challenge is answered; conceded, unanswered or unquoted ones *stand*.
5. Arbiter (no tools): `confirmed` or `revised`.
6. Deterministic acceptance:
   * confirmed: confidence -0.1 per standing challenge (max -0.3);
   * revised, real -> false alarm: needs >= 2 quotes, all grounded, and every worker domain normal;
   * other revisions: >= 50% grounded quotes (ungrounded cost 0.15 each);
   * no surviving challenge: confirmed without an arbiter call.
7. Any phase may fail; the original diagnosis is kept and the reason recorded. `LLMConfigError` always propagates.

Wiring: `DebatedOrchestrator` (orchestrator then debate; harness `llm_debate`), `HierarchicalSwarm(debate=True)`
(harness `llm_swarm_debate`, the commander reviews the debated diagnosis), CLI `--agent debate|swarm-debate`.
`--risk-guard` also applies to accepted revisions.

### Metrics
`summarize` adds `false_positive_rate` (gold false alarms claimed real), `false_negative_rate` (real incidents
claimed false alarm), `gold_false_alarms`, `debate_revised`, `debate_rejected`. Rates are `null` when undefined.
`eval.compare` prints FP/FN per run and which incidents were fixed or newly broken between two runs.

### Dataset v2 (INC-021..028), added before any measurement
Four hard false alarms (planned node drain, nightly batch, exporter sentinel, approved load test) and four real
incidents that look harmless (post-maintenance config regression, partial secret rotation, slow-burn leak,
consumer stall behind a batch job). Twins pair each false alarm with a real incident. INC-001..020 are unchanged
(hashes pinned in `tests/dataset_v1_hashes.json`). Splits: dev 14, test 14, four new incidents each.
Change records are timestamped before the window because the deploy tool only shows records before it.

## Non-goals
No remediation is executed; no change to the Stage 6/7 prompts; no new tools (a timeline tool is a measured-need
follow-up).

## Acceptance
* Tests: `tests/test_llm_debate.py`, `tests/test_dataset_v2.py`, FP/FN tests in `tests/test_eval_extras.py`; the
  debate module is mutation-checked (policy constants, grounding gates, tool access, failure paths).
* Measurement (user side, see `docs/EVAL.md`): `llm_orchestrator` vs `llm_debate` on dev, with FP and FN reported.
  Success is a lower FP rate **without** a higher FN rate. Otherwise the debate is not kept as default.

## Known limits
With 3 dev false alarms (2 in test) FP-rate differences are indicative only; the bootstrap CI will be wide.
