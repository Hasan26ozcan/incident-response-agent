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
