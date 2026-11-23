# Proposal: Stage 21c — End-to-end pipeline

- **Status:** Implemented
- **Depends on:** 17 (durable), 18 (HITL), 16 (sandbox), 20 (security), 21 (observability), 21b (lab)

## Problem
The parts worked separately; nothing ran an incident through all of them, and tool output reached the model unfiltered (INJ-09).

## Decisions
1. A `diagnoser` hook in the durable workflow; the default stays rule-based.
2. Hardening of tool output is a scoped switch, on only inside the pipeline.
3. Default **shadow** mode: advice and dry-runs only; `assist` waits for the sealed human approval.
4. Conservative diagnosis→command mapping; ambiguity and dependency failures never mutate anything.
5. Per-run artifacts: trace, journal, audit log, dashboard, investigation JSON.

## Out of scope
Real-model quality measurement (step 3), service/API layer (step 5), production data connectors, auto-approval.

## Acceptance
`pytest tests/test_pipeline.py` passes; a memory-leak diagnosis ends in a dry-run restart with `changed: false`; the injection
end-to-end test passes in both directions; the security suite has 0 vulnerable.
