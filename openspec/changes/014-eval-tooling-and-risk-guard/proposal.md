# 014 — Eval tooling and deterministic risk guard

## Why
Stages 6-7 claim improvements; without paired statistics, cost accounting and a risk safety net on a
10-incident dev split, those claims are unfalsifiable. Models also under-classify risk of actions like restarts.

## What
* `eval/compare.py`: load tagged runs, paired bootstrap (seed 0, 10 000 resamples), delta with 95% CI,
  win/loss/tie, cost multiple, small-n warning.
* `eval/judge.py`: optional LLM judge (correct 1.0 / partial 0.5 / incorrect 0) on traces; must be a
  different model than the agent unless explicitly allowed.
* `eval/preflight.py`: verifies chat, tool calling, structured output before spending quota.
* `agents/risk_guard.py`: deterministic floor from `openspec/risk-classification.md`. Only raises the tier,
  never lowers it; negation-aware; applied as post-processing (cached responses reused, zero API cost).
  Opt-in via `risk_guard=True` / `--risk-guard`.
* CLI `diagnose --agent llm|orchestrator|swarm|legacy [--risk-guard]`.

## Non-goals
No new LLM calls in the guard; no lowering of tiers; no change to the gold set or splits.

## Acceptance
* Guard never exceeds gold tier on the 20 gold remediations (0 over-raises) and catches >= 90% of gold HIGH.
* Compare output is deterministic for given inputs. Judge refuses same-model by default.
* Tests: `tests/test_eval_extras.py`, `tests/test_cli.py`; mutation-checked.

## Known limits
Keyword floor misses semantic risk (e.g. "revert CoreDNS config map" classified only MEDIUM). Documented, not hidden.
