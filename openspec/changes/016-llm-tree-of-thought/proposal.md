# 016 — LLM Tree-of-Thought + Plan-and-Solve (Stage 9)

## Why
Roadmap Stage 9: parallel hypothesis branching on complex scenarios, most likely scenario selected, accuracy measured on the gold set. The legacy `tree_of_thought_agent.py` is rule-based, never used an LLM and is not trusted; it is superseded.

## What
`agents/tree_of_thought.py`:
1. **Hypothesize** (no tools): quoted observations + up to N-1 distinct alternatives, each with a plan. The current diagnosis is always H0; near-duplicates (token Jaccard >= 0.6) are dropped.
2. **Branches** (parallel): each executes its plan with read-only tools, then files a `BranchReport` (support/refute findings and plan results with verbatim quotes, explained/contradicted observations).
3. **Score** (deterministic, model cannot set it): support(min(n,4)/4) + 1.5·coverage − 1.5·refutation(min(n,3)/3) − contradiction + 0.5·plan; only grounded items count.
4. **Select**: a challenger must beat H0 by `switch_margin` 0.15; ties keep H0.
5. **Solve**: `RevisedDiagnosis` for the winner under the shared `accept_revision` policy (same as debate); confidence capped at 0.6 when the winner's margin over the runner-up is < 0.15.

Failures keep the original diagnosis; `LLMConfigError` always propagates; degraded base is skipped; optional confidence gate.

## Wiring
`ToTOrchestrator`; `HierarchicalSwarm(tot=True)` (ToT → debate → commander); harness `llm_tot`, `llm_swarm_full`; CLI `--agent tot|swarm-full`.

## Verification
`tests/test_tree_of_thought.py` (conformance, scoring, selection, policy, gating, failures, wiring, harness); 28-mutant check, all killed. Value is decided by the dev-split ablation in `docs/EVAL.md` §4c, not by tests.
