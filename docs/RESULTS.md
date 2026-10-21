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

## Stage 9: Tree-of-Thought

| Run | F1 | Risk acc | FP | FN | Switched/rejected | Cost × |
|-----|----|----------|----|----|-------------------|--------|
| llm_orchestrator (s9_base) | — | — | —/3 | —/11 | n/a | 1.0 |
| llm_tot (s9_tot) | — | — | —/3 | —/11 | —/— | — |
| llm_swarm_full (s9_full) | — | — | —/3 | —/11 | —/— | — |

## Stage 10: Hybrid retrieval (dev split, n=14, hashing embedder, numpy store)

Measured in the development sandbox. Dense = lexical hashing baseline, **not** semantic. Re-run with `st:` to fill the real-embedder rows.

| Query style | Mode | hit@1 | hit@3 | hit@5 | MRR | nDCG@10 |
|-------------|------|------:|------:|------:|----:|--------:|
| log | bm25 | 0.857 | 0.929 | 1.000 | 0.907 | 0.841 |
| log | dense | 0.857 | 1.000 | 1.000 | 0.929 | 0.789 |
| log | hybrid | 0.929 | 1.000 | 1.000 | 0.952 | 0.840 |
| paraphrase | bm25 | 0.857 | 1.000 | 1.000 | 0.929 | 0.819 |
| paraphrase | dense | 0.643 | 0.929 | 0.929 | 0.786 | 0.710 |
| paraphrase | hybrid | 0.786 | 0.929 | 0.929 | 0.867 | 0.780 |
| symptom | bm25 | 0.571 | 0.857 | 0.857 | 0.710 | 0.609 |
| symptom | dense | 0.429 | 0.643 | 0.643 | 0.543 | 0.445 |
| symptom | hybrid | 0.571 | 0.857 | 0.929 | 0.705 | 0.578 |

Paired MRR deltas (hybrid minus bm25): log +0.045, paraphrase -0.061, symptom -0.005; every 95 % CI includes 0.
Honest reading: with a lexical "dense" side, hybrid does not beat BM25 on this set; the gain has to come from a real
embedder (pending). Log queries are saturated on dev (n=14), the `symptom` style is the one with headroom.

## Known measured facts (pre-existing)

* Legacy rule-based orchestrator: F1 0.068, answered 0.15, risk accuracy 0.0, 1 crash (legacy audit).
