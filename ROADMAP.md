# Roadmap — 23 Stages, 8 Phases

Each stage exists to prove exactly one thing. Phases group stages so the
big picture stays visible.

## Phase A — Foundations & Spec (Stage 0–2)

**Stage 0 — Spec & Architecture (OpenSpec)**
Scope, agent responsibility matrix, and risk classification (low / medium /
high risk actions) via an OpenSpec proposal. Includes a short comparison
note on BMAD-METHOD vs Spec Kit (interview prep).
*Output:* `openspec/` directory with an approved proposal.

**Stage 1 — Synthetic Incident Dataset**
15–20 synthetic incident scenarios (CPU/memory exhaustion, cascading
failure, bad deploy, DB connection-pool exhaustion, etc.), plus a gold
eval set and rubric.
*Output:* dataset + evaluation criteria.

**Stage 2 — Repo Skeleton & CI Baseline**
Project structure, dependency management, ruff/bandit/mypy CI set up from
the start (not bolted on later). Sandboxed execution environment
(isolated Docker container).
*Output:* an empty but CI-green repo skeleton.

## Phase B — Agent Fundamentals (Stage 3–5)

**Stage 3 — Single-Agent Skeleton (ReAct)**
ReAct loop, basic log reading/diagnosis.
*Output:* single-agent CLI prototype.

**Stage 4 — Structured Output & Prompt Engineering**
Agent outputs strictly validated via Pydantic schemas + JSON mode/grammars.
Few-shot examples and disciplined system-prompt templating established.
*Output:* schema validation test suite, prompt library.

**Stage 5 — Dynamic Planning & Error Recovery** ✅
Replanning loop when a step fails (rebuild the plan from feedback).
*Output:* test showing recovery from a deliberately injected failure.

## Phase C — Multi-Agent Architecture (Stage 6–9)

**Stage 6 — Orchestrator–Workers**
Triage agent + parallel specialist agents (logs / metrics / deploy
history). State management via LangGraph.
*Output:* parallel worker agents processing a single synthetic incident.

**Stage 7 — Hierarchical Swarm**
Incident Commander agent added; synthesizes workers' findings.
*Output:* end-to-end diagnosis flow (no actions yet).

**Stage 8 — Debate Mechanism** ✅
Root-cause agent vs. "forensic examiner" agent debate.
*Output:* before/after false-positive rate comparison.

**Stage 9 — Tree-of-Thought + Plan-and-Solve** ✅
Parallel hypothesis branching on complex scenarios; most likely scenario
selected.
*Output:* measurable accuracy improvement on the gold set.

## Phase D — Memory & Retrieval (Stage 10–12)

**Stage 10 — Vector DB & Hybrid Retrieval**
Qdrant setup, BM25 + dense + RRF (carrying over prior ARAP experience).
*Output:* working hybrid search layer.

**Stage 11 — Reranking**
Cross-encoder reranker added; retrieval quality measured.
*Output:* before/after relevance comparison report.

**Stage 12 — Episodic Memory & Self-Improvement (Hermes-style)**
Every resolved incident is written up as a post-mortem, embedded, and
added to memory. Reflection loop flags misdiagnoses and updates strategy.
*Output:* measurable improvement (time/accuracy) on second encounter.

## Phase E — Tools & Integration (Stage 13–16)

**Stage 13 — MCP Servers**
Log / metrics / deploy / sandbox script execution — each its own MCP
server.
*Output:* agents can pull data from simulated live systems.

**Stage 14 — MCP Discovery & Resource Lifecycle**
Agents discover which tools are available; context negotiation. Explicit
management of connection open/close, resource subscriptions, and stale
resource cleanup.
*Output:* demo of the discovery mechanism.

**Stage 15 — Notification & Reporting Integration**
Slack notification (mock/real webhook). Post-mortem report auto-published
to a wiki/Confluence page — the realistic, scenario-appropriate stand-in
for the "UI automation" / "CRM-DB integration" spirit of the job posting.
*Output:* an automatically generated, published post-mortem page.

**Stage 16 — Sandbox Code/Shell Execution Safety**
Risky commands run in an isolated environment with an allow-list.
*Output:* isolation verified via sandbox-escape attempt tests.

## Phase F — Orchestration & Resilience (Stage 17–18)

**Stage 17 — Temporal Workflow**
Long-running incident workflow that resumes from interruption.
*Output:* post-interruption resume demo.

**Stage 18 — Human-in-the-Loop & Escalation**
Approval gate for risky actions; escalation on timeout.
*Output:* test scenario covering pending approval and timeout escalation.

## Phase G — Evaluation, Safety, Observability (Stage 19–21)

**Stage 19 — Agentic Eval Harness**
Task completion, efficiency, and safety metrics; regression test suite.
*Output:* eval suite running automatically in CI.

**Stage 20 — Adversarial Testing & Security**
Prompt injection / jailbreak scenarios (deception attempts hidden inside
logs), kill-switch mechanism.
*Output:* security test report.

**Stage 21 — Observability & Cost**
LangSmith/Arize tracing + W&B experiment tracking (distinction: trace =
runtime observability, W&B = experiment/model comparison). Cascade
routing, prompt caching, sliding-window context management.
*Output:* dashboard + cost/latency report.

## Phase H — Packaging (Stage 22)

**Stage 22 — Documentation, Demo, Release**
README, architecture diagram, recorded live demo, LinkedIn material.
*Output:* portfolio-ready repo.

---

## Realism note (lesson learned from vuln-triage)

Finishing all 23 stages at "full production" quality would take months.
Recommended sequencing:

- **Phases A–D (Stage 0–12): build for real.** This alone proves the
  backbone and the most original part of the project (self-improving
  memory, debate, Tree-of-Thought).
- **Phases E–F (Stage 13–18): simulated systems, real mechanics** (mocked
  logs, but a genuinely working Temporal/MCP flow).
- **Phases G–H (Stage 19–22): finish with at least one full end-to-end run.**

Following this order means there's always something demonstrable at every
stage — if the project gets cut short, its portfolio value doesn't
disappear with it.
