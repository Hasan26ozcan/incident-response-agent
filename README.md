# Incident Response Agent

An agentic AI system that triages and diagnoses production incidents, built as a
23-stage, eval-first project: single agent, orchestrator-workers, hierarchical
swarm, retrieval/memory, tooling, durable workflows, human-in-the-loop, and
evaluation/observability.

Every claim in this repo is meant to be backed by a **measured** number on a frozen
gold set. Where a stage has not been measured yet, the table below says so.

## Status (honest)

| Stage | What | State | Measured |
|------:|------|-------|----------|
| 0 | Spec & architecture (OpenSpec) | done | n/a |
| 1 | Synthetic dataset, gold set, rubric (28 incidents after dataset v2) | done | n/a |
| 2 | Repo skeleton, CI (ruff, mypy, bandit, pytest, sandbox image scan) | done | n/a |
| 3 | Single-agent ReAct (LLM, tool calling, grounding) | implemented, hardened | see `docs/RESULTS.md` |
| 4 | Structured output (schema-validated, repair loop) | implemented, hardened | see `docs/RESULTS.md` |
| 5 | Dynamic planning and error recovery (replan on tool failure) | implemented, hardened | n/a (behavioural tests) |
| 6 | Orchestrator-workers (LLM triage, scoped parallel workers) | implemented | pending ablation |
| 7 | Hierarchical swarm (commander, deterministic escalation policy) | implemented | pending ablation |
| 8 | Debate (defender vs forensic examiner with tools, arbiter, deterministic acceptance) | implemented | pending ablation |
| 9 | Tree-of-Thought + Plan-and-Solve (parallel hypothesis branches, deterministic scoring, margin-gated switch) | implemented | pending ablation |
| 10-12 | Hybrid retrieval, reranking, episodic memory | legacy rule-based code, **not yet audited or rewritten** | no |
| 13-22 | MCP, Slack/wiki, sandbox safety, Temporal, HITL, eval CI, security, observability, release | not started | no |

"Implemented" means: code + tests (roadmap-conformance, failure modes, mutation-checked).
It does **not** mean "proven better". The measured column is filled from real runs only
(see `docs/EVAL.md`, `docs/RESULTS.md`).

## Two code lines

* **LLM line (trusted, being measured):** `agents/llm_react_agent.py`, `tool_loop.py`,
  `grounding.py`, `llm_orchestrator.py`, `llm_commander.py`, `llm_debate.py`, `tree_of_thought.py`, `swarm.py`, `risk_guard.py`,
  plus `llm/` (client, cache, fallback, structured output) and `tools/toolbox.py`.
* **Legacy line (rule-based, kept for back-compat):** `react_agent.py`, `orchestrator.py`,
  `worker.py`, `incident_commander.py` and Stage 10-12 modules (and the old `tree_of_thought_agent.py`, superseded by `tree_of_thought.py`). The legacy orchestrator
  measured F1 0.068 / risk accuracy 0.0 on the gold set; do not treat it as evidence.

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
Copy-Item .env.example .env       # then set GROQ_API_KEY (never commit it)
python -m pytest -q
python -m incident_agent diagnose INC-001 --agent swarm --risk-guard
```

`--agent` accepts `llm` (single ReAct), `orchestrator`, `debate`, `tot`, `swarm`, `swarm-debate`, `swarm-full` (debate + ToT), `legacy`. Without
`--agent`, the CLI uses the LLM agent when `GROQ_API_KEY` is set, else the legacy one.

## Evaluation workflow

Short version (full runbook: [`docs/EVAL.md`](./docs/EVAL.md)):

1. `python -m incident_agent.eval.preflight` checks the model supports chat, tool calling and structured output.
2. Run baselines/agents on the **dev** split, tagged: `python -m incident_agent.eval.harness --agent llm_react --split dev --tag v0_dev --trace`.
3. Compare runs with paired bootstrap CIs: `python -m incident_agent.eval.compare <run_a> <run_b>`.
4. Optional LLM judge with a *different* model family: `python -m incident_agent.eval.judge --tag <tag> --split dev`.
5. The **test** split is opened only at milestones (each opening is logged in `eval/test_openings.log`).

## Layout

```
data/            generator + 28 synthetic incidents (INC-001..028; 021-028 are the Stage 8 false-alarm/trap set)
eval/gold/       gold answers (kept apart from data/), splits.json (frozen dev/test)
eval/results/    tagged run results (git-ignored); eval/traces/ per-incident traces (git-ignored)
src/incident_agent/
  llm/           client, cache, fallback, tracking, structured output, scripted fake
  tools/         IncidentToolbox (validated tool args, per-worker allow-lists)
  agents/        LLM line + legacy line (see above)
  eval/          harness, compare, judge, preflight
  schemas/ prompts/ retrieval/ workflows/ memory/
openspec/        spec, risk classification, per-change proposals (changes/NNN-*)
docs/            EVAL.md (runbook), RESULTS.md (measured numbers), AUDIT-STAGES-0-5.md
tests/           pytest suite
```

## Why OpenSpec

Non-trivial changes start as a proposal in `openspec/changes/`. See
`openspec/comparison-bmad-speckit-openspec.md` for the rationale.

## License

MIT, see [`LICENSE`](./LICENSE).
