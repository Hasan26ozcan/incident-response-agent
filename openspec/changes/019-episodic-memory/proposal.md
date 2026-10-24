# 019 - Episodic memory (Stage 12)

## Why
The legacy memory was a JSON-file store with string-equality "correctness", a manufactured `accuracy_after` and gold text leaking into stored strategy (see `docs/AUDIT-STAGES-10-12.md`). The roadmap asks for measurable improvement on a second encounter, which the existing dataset cannot show.

## What
- `memory/episodic.py`: episodes (observation, resolved class, first diagnosed class, deterministic reflection), JSONL persistence in an explicit directory, hybrid recall over observations, class prediction with a dense-cosine gate, injectable clock.
- `data/make_recurrences.py`, `data/recurrence/`, `eval/recurrence_gold.json`: 14 recurrences of dev failures on different services; pinned data untouched.
- `eval/memory_eval.py`: replay protocol and kb vs memory vs combined comparison, novel-class control.

## Design decisions
- Resolved class is written only after the diagnosis; classes are compared as a controlled vocabulary, never as free text.
- "Self-improvement" = remembered outcomes and lessons that change recall. No prompt, weight or threshold is rewritten.
- Gating uses dense cosine (calibrated), not RRF scores (rank-based, nearly flat). Threshold chosen on dev only.
- Agents are not wired to memory yet; doing so changes prompts and invalidates caches and needs its own flagged change.

## Out of scope
Agent integration, LLM-written post-mortems, memory decay/consolidation.

## Acceptance
Tests incl. mutation check; recurrence result with confidence interval and a novel-class control; no writes inside `src/`.
