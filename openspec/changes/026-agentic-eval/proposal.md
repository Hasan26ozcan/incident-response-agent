# Proposal: Stage 19 — Agentic Eval Harness

- **Status:** Implemented
- **Stage:** 19 of 23
- **Phase:** G — Evaluation & Security
- **Depends on:** Stages 3, 10–12 (existing evals), 16 (sandbox), 17 (durable runs), 18 (approval gate)

## 1. Problem

Earlier stages are individually tested, but nothing measured the *system*: does an incident run complete, how much
does it cost, and does it stay safe when humans stay silent, attackers act or workers die? Nothing ran in CI as a
regression gate either.

## 2. Scope

In: task completion / efficiency / safety metrics, 11 scenarios over the real engine and gate, negative controls,
baseline gating, a CLI and a CI workflow; the existing diagnosis, retrieval and memory evals folded in as sections.
Out: adversarial content inside logs and the kill-switch (Stage 20), observability and cost tracking (Stage 21),
wiring anything into the agents, the test split.

## 3. Decisions

1. **Safety is judged on the wire.** A recording backend observes every `apply=true` call; the verdict does not trust the
   audit log or the run's own report.
2. **Zero tolerance for safety, tolerance elsewhere.** Quality may move ±0.02, cost ×1.15.
3. **Negative controls are part of the suite.** A blind metric fails the build.
4. **Known gaps are reported, not hidden.** The forged-signal bypass is a first-class scenario that does not gate, and a
   test forces a docs/baseline update when it closes.
5. **Simulated time, real components.** Only the clock, the isolation backend and the humans are simulated.
6. **Dev split only**, enforced in code.

## 4. Acceptance

- `python -m incident_agent.agenteval run` exits 0 on a healthy tree and non-zero when any gate rule is violated.
- Each negative control is flagged; each safety invariant has a test that fails when it is disabled.
- `.github/workflows/eval.yml` runs the suite on push, pull request and nightly.
