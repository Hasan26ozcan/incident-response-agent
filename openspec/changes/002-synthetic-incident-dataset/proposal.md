# Proposal: Stage 1 — Synthetic Incident Dataset

- **Status:** Approved
- **Stage:** 1 / 22
- **Phase:** A — Foundations & Spec
- **Depends on:** Stage 0 (`openspec/proposal.md`, `openspec/risk-classification.md`,
  `openspec/agent-responsibility-matrix.md`)

## 1. Problem / Motivation

Every stage from Stage 3 onward needs incidents to diagnose. Without a
dataset built up front, each later stage would improvise its own ad-hoc
test data, making results incomparable across stages and impossible to use
as a regression suite (Stage 19). This stage produces the one dataset every
later stage diagnoses, debates, remembers, and is scored against.

## 2. Scope

- 20 synthetic incident scenarios (`data/incidents/INC-001`…`INC-020`),
  each with a log store, metrics store, and deploy-history store — the
  exact three data sources the Log/Metrics/Deploy-History agents read per
  the Stage 0 agent responsibility matrix.
- A gold answer per scenario (`eval/gold/INC-0NN.json`), kept structurally
  separate from the agent-visible data (see §4).
- A scoring rubric (`eval/rubric.md`) usable by a human reviewer now and
  the automated eval harness later (Stage 19).
- A human-readable index (`eval/gold_set.md`), mechanically generated from
  the gold files so it can't drift out of sync.
- A deterministic, stdlib-only generator (`data/generate_dataset.py`) and
  a validator (`eval/validate_dataset.py`), so the dataset is reproducible
  and its integrity is checked mechanically, not by eyeballing.

Out of scope for this stage: any agent that actually reads this data
(Stage 3+), and the eval harness automation itself (Stage 19) — this stage
only produces the rubric it will use.

## 3. Coverage decisions

The roadmap named a non-exhaustive category list ("CPU/memory/cascading
failure/deploy error/DB pool exhaustion/etc."). To make sure "etc." didn't
turn into a gap, coverage was chosen deliberately:

| Requirement | How it's met |
|---|---|
| 15–20 scenarios | 20 scenarios, `INC-001`–`INC-020` |
| Named categories (CPU, memory, cascading failure, bad deploy, DB pool) | `INC-001`, `INC-002`, `INC-003`, `INC-004`, `INC-005` |
| Breadth beyond the named list, since production incidents aren't limited to five categories | 14 further categories: disk, network partition, third-party outage, DNS, TLS expiry, MQ backlog, k8s crashloop, autoscaling failure, secret rotation, config drift, load-balancer misconfig, GC pause storm, N+1 query, false alarm |
| Something for Stage 8 (debate) to adjudicate | 2 distractor pairs with genuinely similar surface symptoms and different root causes: INC-005↔INC-018, INC-007↔INC-017 |
| Something for Stage 9 (ToT / Plan-and-Solve) to branch on | 5 `hard`-difficulty scenarios with ambiguous or misleading immediate signals (INC-003, INC-007, INC-013, INC-015, INC-017) |
| Something for Stage 12 (episodic memory) to demonstrate improvement on | 1 recurrence pair, same bug pattern on a different service: INC-002 → INC-020 |
| Something for false-positive handling (referenced by `risk-classification.md`'s medium-risk governance and the rubric's hard gate) | 1 false-alarm scenario with zero real anomaly signal: INC-019 |
| A rubric that reflects the Stage 0 risk classification, not just diagnostic accuracy | `eval/rubric.md` §6 cross-checks every scored run against `openspec/risk-classification.md` and the agent responsibility matrix as a hard gate |

## 4. Key design decision: gold answers live outside `data/incidents/`

Gold answers are written to `eval/gold/`, never under `data/incidents/`,
even though it would be simpler to keep them together. Rationale: from
Stage 13 onward, agents get MCP-based tool access to these stores, and tool
access is easy to over-scope by accident (e.g. a filesystem MCP server
pointed at `data/` instead of `data/incidents/`). Structural separation
means an over-scoped tool grant leaks nothing, rather than relying on every
future agent prompt correctly saying "don't read the gold file." This is
checked mechanically by `eval/validate_dataset.py` (leakage check) on
every regeneration, not just asserted here.

## 5. Acceptance criteria

- [x] 20 scenarios generated, covering 19 categories (one, `memory_leak`,
      deliberately repeated for the recurrence-pair test)
- [x] Every scenario has `meta.json`, `logs.log`, `metrics.json`,
      `deploys.json` under `data/incidents/<id>/`
- [x] Every scenario has a corresponding `eval/gold/<id>.json` with
      `root_cause`, `evidence`, `recommended_remediation`,
      `remediation_risk_tier` (classified against
      `openspec/risk-classification.md`), and `risk_rationale`
- [x] `eval/rubric.md` defines a composite score plus two hard gates
      (false-positive handling, safety/risk-tier compliance)
- [x] `eval/gold_set.md` is mechanically generated
      (`eval/generate_gold_set_md.py`), not hand-maintained
- [x] `eval/validate_dataset.py` passes: correct scenario count, no
      leakage into `meta.json`, false-alarm scenario has zero ERROR/WARN
      log lines, no empty metric series, gold/incident directories match
      1:1, `recurrence_of` references resolve
- [x] Generation is deterministic (seeded per scenario) and stdlib-only,
      so it runs standing on its own ahead of Stage 2's dependency
      management

## 6. Risks / open questions carried into later stages

- The generated log/metric realism is good enough for Stage 3–9 agent
  reasoning, but not adversarially hardened — Stage 20 (adversarial
  testing) will likely need its own dedicated injection scenarios rather
  than reusing this dataset as-is.
- Metric anomaly shapes are currently limited to "ramp" and "step"; if
  a later stage needs a scenario with a genuinely oscillating or
  self-recovering signal, `gen_metric_series` will need a new shape —
  flagged here rather than over-building shapes with no current use.
- The step-budget numbers in `eval/rubric.md` §4 (Efficiency) are
  placeholders ("tune per Stage 3/6 tooling once real step costs are
  known") — deliberately left approximate since no agent has run against
  this data yet.
