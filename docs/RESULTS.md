# Measured results

Nothing here is filled from estimates. Fill each table from `python -m incident_agent.eval.compare ... --markdown`.

Model: _(set GROQ_MODEL)_  Date: _(run date)_  Split: dev (n=14 after dataset v2)

## Baselines

| Run (tag) | F1 | Evidence recall | Risk acc | False-alarm acc | Answered | Crashed | LLM calls |
|-----------|----|-----------------|----------|-----------------|----------|---------|-----------|
| react_baseline (legacy_all, all 20) | — | — | — | — | — | — | — |
| llm_react (v0_dev) | — | — | — | — | — | — | — |

## Stage 6: single vs orchestrator

| Run | F1 | ΔF1 vs single (95% CI) | Risk acc | Cost × |
|-----|----|------------------------|----------|--------|
| llm_react (s6_single) | — | — | — | 1.0 |
| llm_orchestrator (s6_orch) | — | — | — | — |

## Stage 7: swarm / commander revision

| Run | F1 | Risk acc | False-alarm acc | Cost × |
|-----|----|----------|-----------------|--------|
| llm_swarm_norevise (s7_norev) | — | — | — | — |
| llm_swarm (s7_swarm) | — | — | — | — |
| llm_swarm + risk guard | — | — | — | — |

## Stage 8: debate

| Run | F1 | Risk acc | FP (false alarms called real) | FN (real called false alarm) | Cost × |
|-----|----|----------|-------------------------------|------------------------------|--------|
| llm_orchestrator (s8_base) | — | — | —/3 | —/11 | 1.0 |
| llm_debate (s8_debate) | — | — | —/3 | —/11 | — |
| llm_swarm_debate (s8_swarm_debate) | — | — | —/3 | —/11 | — |

Keep the debate only if FP drops and FN does not rise. With 3 false alarms in dev the evidence is indicative.

## Known measured facts (pre-existing)

* Legacy rule-based orchestrator: F1 0.068, answered 0.15, risk accuracy 0.0, 1 crash (legacy audit).
