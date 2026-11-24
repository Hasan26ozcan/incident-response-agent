# Proposal: Stage 21d — Real-model measurement

- **Status:** Implemented (tooling); results produced by the owner
- **Depends on:** 3 (harness), 21 (observability, pricing), 21b (lab), 21c (client injection)

## Problem
No agent had ever been measured with a real provider on realistic data, and the cascade's saving was only a mechanics test.

## Decisions
1. Campaigns of named configurations (agent × model or cascade), runnable and resumable with a local `.env`.
2. No disk cache; per-incident tokens, latency and priced cost from spans.
3. Report with bootstrap intervals, paired comparison and explicit small-sample language.
4. Dev split and lab only; the test split is unreachable here.
5. Keys by name only in all messages.

## Out of scope
LLM judge scoring, test-split opening, long soak runs, provider failover experiments.

## Acceptance
`pytest tests/test_measure.py` passes with fake providers; `measure plan` works offline; a run with real keys produces
`docs/MEASUREMENTS.md`.
