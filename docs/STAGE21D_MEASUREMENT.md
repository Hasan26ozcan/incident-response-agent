# Stage 21d — Real-model measurement

Everything before this ran against scripted models. This stage is how **you** measure real providers on the dev split
(14 dataset incidents) and the five lab captures — quality, tokens, latency, priced cost, and whether a cascade really saves money.
The test split is **not** reachable from here (it is opened only at milestones, with the harness's own logged flag).

## Keys (never in chat)
Put these in `.env` in the project root (it is gitignored); only the names matter here:
```
GROQ_API_KEY=...        # gpt-oss-20b / gpt-oss-120b
MISTRAL_API_KEY=...     # optional: mistral-large-latest
LLM_PROVIDERS=groq      # only for the older harness commands
```
A key that was ever pasted into a chat is compromised: revoke it and create a new one.

## Run it (PowerShell, project root, `.venv` active)
```
python -m incident_agent.eval.preflight                          # 3 tiny calls: key, model, tool calling, structured output
python -m incident_agent.measure plan cascade                    # what will run; spends nothing
python -m incident_agent.measure run smoke --sets lab --pace 2   # 5 incidents, one config: first real result
python -m incident_agent.measure run cascade --sets dev,lab --pace 2
python -m incident_agent.measure report cascade                  # writes docs/MEASUREMENTS.md
```
Free tiers rate-limit. If a run stops ("stopped ... Re-run with --resume") just run the same command again with `--resume`:
finished incidents are kept, crashed ones are retried, and the result file is saved after every incident.
Other campaigns: `models` (20b vs 120b vs Mistral Large), `agents` (react vs orchestrator vs swarm). `--only <config>` runs one.

## What you get (`eval/measure/<campaign>/`)
`<config>__<set>.json` (scores, per-incident tokens/latency/cost), `.trace.jsonl` (every model call as a span),
`.observability.html` (dashboard), and `docs/MEASUREMENTS.md`: F1 with 95 % bootstrap intervals, paired wins/ties/losses against a
reference config, cost per incident from the verified price table, latency, and for the cascade: the share of calls that
reached the strong model and the real saving versus always-strong.

## How to read it
* The quality metric is token-overlap F1 against the gold root cause: crude, but the same one used since Stage 3. With 14 + 5
  incidents, differences of a few points are noise; the report says so.
* A cascade saving only counts if quality did not drop (compare the F1 row and W/T/L). If the cheap model's output passes the
  default gate (non-empty, schema-valid), the cascade rarely escalates and mostly measures the cheap model.
  A stricter gate (e.g. escalate when the diagnosis cites no evidence) is the next knob.
* No disk cache is used, so latency and cost are real. Re-running costs quota again.
* Models without a verified price show as "unpriced", never as free.
* The rule-based baseline scores 0 of 5 on the lab captures (Stage 21b); this is where the LLM agents should be compared to that.

## Limits
Dataset incidents are still synthetic; lab incidents are real but come from one toy application; scoring is automatic, not a judge.
This stage ships the measurement tooling and tests (fake providers); the numbers will exist only after you run it.
