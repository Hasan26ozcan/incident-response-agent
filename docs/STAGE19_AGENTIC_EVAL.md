# Stage 19 — Agentic Eval Harness

*Roadmap output:* an eval suite running automatically in CI. → `.github/workflows/eval.yml`, gated by
`eval/agentic/baseline.json`.

```
python -m incident_agent.agenteval list          # sections, scenarios, negative controls
python -m incident_agent.agenteval run           # run on the dev split and gate against the baseline
python -m incident_agent.agenteval run --only agentic,controls
python -m incident_agent.agenteval run --update-baseline   # deliberate, full suite only
```

Exit codes: `0` pass, `1` regression, `2` error. The full report is written to `eval/results/agentic.json`.

## What it covers

| Section | What is measured | Built in stage |
|---|---|---|
| `planner` | the command the workflow proposes per incident (pinned), mutating plans on gold false alarms (must be 0) | 17 |
| `agentic` | **end-to-end runs of the real durable engine + approval gate** under 11 scenarios | 16, 17, 18 |
| `controls` | deliberately broken variants the metrics must flag | 19 |
| `security` | the Stage 20 attack suite (31 attacks; see `docs/SECURITY_REPORT.md`) | 20 |
| `diagnosis` | R0 rule-based ReAct vs gold (F1, evidence, risk, false-alarm accuracy) | 3 |
| `retrieval` | hybrid retrieval MRR / hit@1 for log, paraphrase and symptom queries (hashing embedder) | 10–12 |
| `memory` | KB-only vs KB+episodic-memory accuracy on recurring incidents | 12 |

## The three metric families

* **Task completion** – the share of runs whose outcome (final status, executed mode, number of applied changes, gate
  outcome) equals what a correct system produces for that scenario. The expectation table is in `scenarios.py`.
* **Efficiency** – journal events, sandbox backend calls and simulated seconds per run. A run may cost at most
  1.15× the baseline.
* **Safety** – invariants judged from the **backend wire** (a recording backend sees every call with `apply=true`),
  not from the run's own report: nothing applied without a gate approval; nothing applied twice; a read-only plan never
  applies; an applied change always names its approver. The tolerance is zero.

## Scenarios (mutating plans; read-only plans get one run)

approve · reject · silence (all escalation levels time out) · self-approval · outsider · wrong fingerprint then correct
approval · crash after planning · crash inside `execute` · backend fault *after* the change · cancel while waiting ·
**forged signal** (a known gap in Stage 19, closed in Stage 20).

Time is simulated (`SimClock`), so a 20-minute escalation chain takes microseconds and every number is deterministic
(two runs produce byte-identical reports).

## Negative controls

An eval that cannot fail proves nothing, so three regressions someone could plausibly introduce are run on every
suite and **must** be flagged:

| Control | Regression | Flagged by |
|---|---|---|
| `auto_approve` | execute mints its own approval instead of waiting for a human | safety |
| `retrying_execute` | execute retried with a fresh nonce; a fault after the change repeats it | safety |
| `no_approval_wait` | the approval wait is removed (safe, but the task no longer completes) | completion |

If a control stops being flagged, the gate fails: the metric went blind.

## Gating rules

Safety: zero violations. Completion: not below baseline, every scenario must meet its expectation. Efficiency:
≤ 1.15× baseline. Planner: no mutation of a false alarm; any changed plan fails until re-baselined on purpose.
Diagnosis / retrieval / memory: not more than 0.02 below baseline. `--update-baseline` refuses a failing system and
anything but the full suite on the full dev split.

## Honest limits

* **Forged approval signal: closed in Stage 20.** The wait used to accept any `approval` signal written to the run
  directory; decisions are now sealed by the gate and verified in `execute` (`docs/STAGE20_SECURITY.md`). The
  *known-gap* mechanism (reported, never gated, a test forces the docs to be updated when it closes) stays in the
  harness for the next weakness; at the moment no scenario uses it.
* The isolation backend is a plain subprocess that *claims* the guarantees; real isolation is Stage 16's selftest.
  This stage evaluates the orchestration and approval logic, not the sandbox.
* Only 4 of 14 dev incidents get mutating plans, and the planner is a keyword table. The suite pins its output, it
  does not claim it is good. Diagnosis quality of the R0 baseline is weak (F1 0.114, false-positive rate 1.0 on false
  alarms in the Stage 3 metric); the gate only prevents it getting worse.
* Dev split only (14 incidents). The test split is never opened here and `run --ids` rejects non-dev ids.
* Nothing here is wired into the agents; real LLM diagnosers are evaluated by `eval/harness.py` as before.
* The GitHub workflow is verified by YAML parse and by running the same command locally, not by a real CI run.
