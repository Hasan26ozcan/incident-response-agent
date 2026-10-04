# Audit of Stages 0-5 (and what was changed)

| Stage | ROADMAP requirement | State found | Fix in this change |
|---|---|---|---|
| 0 Spec | proposal, risk tiers, responsibility matrix | present and sound | none |
| 1 Dataset | 15-20 scenarios + gold + rubric | present; logs often state the cause in plain words (e.g. "disk full"), so tasks are easier than real incidents | **open**: add ambiguous/noisy variants (see below) |
| 2 CI | CI green from day one | `ruff` 42 errors, 16 files unformatted, bare `mypy` had no target, `torch` hard dependency used nowhere | ruff/format/mypy/bandit/pytest all green; mypy `files=["src"]`; torch/qdrant moved to optional `[ml]` extra; `openai` added |
| 3 ReAct | LLM-driven observe-reason-act loop | no LLM; 4 hard-coded string matches; answered 5 of 20 incidents | `agents/llm_react_agent.py` + `llm/` layer + incident-scoped read-only toolbox |
| 4 Structured output | Pydantic outputs, JSON mode/grammars, few-shot | schemas fine; JSON mode never used (no LLM) | `generate_structured` (json_schema + validation + repair loop); few-shot example from outside the dataset; evidence grounding check |
| 5 Planning/recovery | plan, replan on failure | planning over fixed steps; "LLM integration out of scope" | LLM writes a structured plan; on tool failure the tool is withdrawn and the LLM replans; fault-injection tests |

Baseline (rule-based agent, eval harness): answered 25%, root-cause F1 0.08, risk accuracy 5%.
`legacy` agent is kept so the LLM agent's improvement is a real before/after.

## Design note
Groq's docs state tools and structured outputs cannot be combined in one request, so the agent
investigates with tools and produces the final answer in a separate schema-constrained call.

## Still open
- Stage 1 realism: gold root causes should require inference, not keyword reading.
- LLM scores have not been measured yet (needs GROQ_API_KEY): `python -m incident_agent.eval.harness --agent llm_react`.
