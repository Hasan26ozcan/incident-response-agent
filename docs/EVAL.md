# Evaluation runbook

All commands run from the project root with `.venv` active (PowerShell). Free API only (Groq).
Set `GROQ_API_KEY` in `.env` (never commit it) and `GROQ_MODEL` to the model you want measured.

## 0. Ground rules

* Develop and tune on the **dev** split only (INC-001, 002, 003, 005, 007, 008, 010, 012, 015, 019, 021, 022, 025, 026).
* The **test** split (004, 006, 009, 011, 013, 014, 016, 017, 018, 020, 023, 024, 027, 028) is opened only at milestones.
  Every opening is appended to `eval/test_openings.log`. Do not tune on it.
* Every run gets a `--tag`. Results land in `eval/results/<agent>__<tag>.json`; `--trace` also
  saves per-incident traces in `eval/traces/<tag>/`.
* n = 14 per split is small: treat differences below the bootstrap CI as noise.

## 1. Preflight (free, ~3 calls)

```powershell
python -m incident_agent.eval.preflight
python -m incident_agent.eval.preflight --judge     # also checks JUDGE_MODEL
```
Exit code 0 only if chat, tool calling and structured output all work on the model.

## 2. Baselines

```powershell
python -m incident_agent.eval.harness --agent react_baseline --tag legacy_all
python -m incident_agent.eval.harness --agent llm_react --split dev --tag v0_dev --trace
```

## 3. Stage 6 ablation (single agent vs orchestrator)

```powershell
python -m incident_agent.eval.harness --agent llm_react --split dev --tag s6_single --trace
python -m incident_agent.eval.harness --agent llm_orchestrator --split dev --tag s6_orch --trace
python -m incident_agent.eval.compare llm_react__s6_single llm_orchestrator__s6_orch
```

## 4. Stage 7 ablation (swarm with and without commander revision)

```powershell
python -m incident_agent.eval.harness --agent llm_swarm_norevise --split dev --tag s7_norev --trace
python -m incident_agent.eval.harness --agent llm_swarm --split dev --tag s7_swarm --trace
python -m incident_agent.eval.compare llm_orchestrator__s6_orch llm_swarm_norevise__s7_norev llm_swarm__s7_swarm
```

## 4b. Stage 8 ablation (debate before/after, false positives and false negatives)

The dataset now has 28 incidents (dev 14, test 14); run the earlier steps again on the new dev split if you
measured before updating (tags are cheap). Orchestrator calls are cached, so the debate run only pays for the
debate itself.

```powershell
python -m incident_agent.eval.harness --agent llm_orchestrator --split dev --tag s8_base --trace
python -m incident_agent.eval.harness --agent llm_debate --split dev --tag s8_debate --trace
python -m incident_agent.eval.compare llm_orchestrator__s8_base llm_debate__s8_debate
python -m incident_agent.eval.harness --agent llm_swarm_debate --split dev --tag s8_swarm_debate --trace
```
Read the FP/FN block first: the debate is only worth keeping if false positives drop and false negatives do
not rise. Traces (`eval/traces/s8_debate/INC-xxx.json`, key `debate`) show every challenge, rebuttal, the arbiter
ruling, discarded challenges and notes for each incident.

## 4c. Stage 9 ablation (Tree-of-Thought + Plan-and-Solve)

Orchestrator calls are cached, so the ToT run only pays for hypothesize + branches + solve.

```powershell
python -m incident_agent.eval.harness --agent llm_orchestrator --split dev --tag s9_base --trace
python -m incident_agent.eval.harness --agent llm_tot --split dev --tag s9_tot --trace
python -m incident_agent.eval.compare llm_orchestrator__s9_base llm_tot__s9_tot
python -m incident_agent.eval.harness --agent llm_swarm_full --split dev --tag s9_full --trace
```
Keep ToT only if F1 / risk accuracy improve (or FP drops) without FN rising, and the cost multiplier is acceptable.
Traces (`eval/traces/s9_tot/INC-xxx.json`, key `tot`) hold observations, every branch with plan, transcript,
score parts and grounded findings, the winner, margin, verdict (`kept|switched|switch_rejected|skipped|gated_off|error`)
and `original_root_cause`. Cost control: `TreeOfThought(skip_if_confidence_at_least=...)` skips confident diagnoses.

## 4d. Stage 10 retrieval evaluation (no LLM calls, no API cost)

```powershell
python -m incident_agent.eval.validate_kb
python -m incident_agent.eval.retrieval_eval --split dev --tag s10_hash
python -m incident_agent.eval.retrieval_eval --split dev --store qdrant --tag s10_hash_qdrant
pip install -e ".[ml]"
python -m incident_agent.eval.retrieval_eval --split dev --embedder st:BAAI/bge-small-en-v1.5 --tag s10_bge
```
Three query styles: `log` (built from logs/metrics/deploys), `paraphrase` (an engineer's description) and `symptom`
(deliberately avoids runbook vocabulary; the headroom set). The `hashing` embedder is lexical, so its "dense" column
is a baseline, not semantic search: only an `st:` run says whether dense retrieval adds anything. Tune (weights,
`--rrf-k`, `--depth`) on dev only; `--split test` is logged in `eval/test_openings.log`. Results:
`eval/results/retrieval__<tag>.json` (per-incident rows, summary, paired deltas, KB fingerprint).
The KB and the queries are synthetic and written by the same author, so absolute numbers are an upper bound.

## 4e. Stage 11 reranking evaluation (no LLM calls, no API cost)

```powershell
python -m incident_agent.eval.retrieval_eval --split dev --reranker overlap --tag s11_overlap
pip install -e ".[ml]"
python -m incident_agent.eval.retrieval_eval --split dev --embedder st:BAAI/bge-small-en-v1.5 --reranker ce:cross-encoder/ms-marco-MiniLM-L-6-v2 --tag s11_ce
python -m incident_agent.eval.retrieval_eval --split dev --embedder st:BAAI/bge-small-en-v1.5 --reranker ce --alpha 0.3 --tag s11_ce_a03
```
The report adds a `rerank` row and `rerank_vs_hybrid_*` paired deltas with bootstrap CIs. Keep the reranker only if
MRR/hit@1 improve with a CI clear of zero on dev. Choose `--alpha`/`--candidates` on dev only, and do not hunt for the
best alpha on 14 incidents: pick from the pre-registered values 0, 0.3, 0.5. `overlap` is lexical, so only `ce:` answers
the neural-reranker question. Set `RERANK_CACHE_DIR` to cache scores between runs.

## 4f. Stage 12 episodic-memory evaluation (no LLM calls, no API cost)

```powershell
python data/make_recurrences.py          # only to regenerate; the files are committed and a test checks they match
python -m incident_agent.eval.memory_eval --tag s12_hash
python -m incident_agent.eval.memory_eval --embedder st:BAAI/bge-small-en-v1.5 --tag s12_bge
python -m incident_agent.eval.memory_eval --queries test --tag s12_control   # logged test opening; milestone only
```
Protocol: dev incidents are replayed in order (predict first, then write the resolved episode); each recurrence is
classified by the knowledge base alone, by memory alone and by `combined` (memory when the best episode's dense cosine
is at least `--min-similarity`, else the knowledge base). Read: accuracy, `rescued`/`misled`, coverage and precision of
memory, the first-encounter-wrong subset, and the novel-class control (memory must stay quiet on a class it has never
seen). `--min-similarity 0.6` was chosen on dev only for the hashing embedder; for `st:` re-derive it with a dev-only run
(top cosine on dev incidents without their own episode versus on recurrences) before trusting it. The recurrence set is a
synthetic variant of the same generator, so results are an upper bound.

## 5. Risk guard (zero API cost)

Same command plus `--risk-guard` and a new tag. LLM responses come from the cache, so this only
re-applies the deterministic risk floor:

```powershell
python -m incident_agent.eval.harness --agent llm_swarm --split dev --tag s7_swarm_guard --risk-guard
python -m incident_agent.eval.compare llm_swarm__s7_swarm llm_swarm__s7_swarm_guard
```

## 6. LLM judge (optional)

Set `JUDGE_MODEL` (and `JUDGE_PROVIDER=groq|cerebras`) to a model of a **different family** from the
agent. The judge refuses the same model unless `--allow-same-model`.

```powershell
python -m incident_agent.eval.judge --tag s7_swarm --split dev
python -m incident_agent.eval.compare llm_swarm__s7_swarm
```

## 7. Recording results

Paste `compare --markdown` output into `docs/RESULTS.md` with the model name, date and tags.
Numbers not produced by a run do not belong there.

## Metrics

root_cause_f1, evidence_recall, risk_accuracy, false_alarm_accuracy, false_positive_rate, false_negative_rate,
answered_rate, crashed, degraded,
llm_calls, prompt/completion tokens. `compare` adds paired-bootstrap 95% CIs (seed 0, 10 000 resamples),
win/loss/tie counts and the cost multiple versus the first run.
