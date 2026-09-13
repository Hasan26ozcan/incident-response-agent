# Proposal: Stage 0 — Spec & Architecture

- **Status:** Approved
<<<<<<< HEAD
- **Stage:** 0 of 23
=======
- **Stage:** 0 / 22
>>>>>>> 60eb90cfe3ed360fa4b33aae3322fe3b15f103ef
- **Phase:** A — Foundations & Spec

## 1. Problem / Motivation

Production incident response today relies on humans manually correlating
logs, metrics, and deploy history under time pressure. This project builds
an **agentic incident-response system** that automates triage and
root-cause diagnosis, and — later — safely assists with remediation under
human supervision. The goal is a portfolio project that demonstrates the
full modern agentic AI stack: single-agent reasoning, multi-agent
orchestration, retrieval-augmented memory, tool use via MCP, durable
workflows, human-in-the-loop safety, and rigorous evaluation.

## 2. Scope of this stage

Stage 0 produces **no application code**. It produces the spec artifacts
that every later stage will be built and reviewed against:

- This proposal (scope, architecture overview, acceptance criteria)
- The agent responsibility matrix
- The risk classification of agent actions
- A short comparison note on spec-driven-development methodologies

Explicitly out of scope for Stage 0: dataset creation (Stage 1), repo/CI
skeleton (Stage 2), and any agent implementation (Stage 3+).

## 3. Architecture overview (target end-state)

The system converges, by Phase C, on a **hierarchical multi-agent
architecture**:

```
                     ┌─────────────────────┐
                     │  Incident Commander  │  (Stage 7)
                     └──────────┬───────────┘
                                │ synthesizes findings
                     ┌──────────┴───────────┐
                     │     Triage Agent      │  (Stage 6)
                     └──────────┬───────────┘
              ┌─────────────────┼─────────────────┐
              │                 │                 │
      ┌───────▼──────┐  ┌───────▼──────┐  ┌───────▼────────┐
      │  Log Agent    │  │ Metrics Agent │  │ Deploy-History  │   (Stage 6, parallel workers)
      └───────────────┘  └───────────────┘  │     Agent       │
                                             └─────────────────┘
              │
      ┌───────▼─────────────────┐
      │ Root-Cause Agent  ⇄      │  (Stage 8 — debate)
      │ Forensic-Examiner Agent  │
      └───────────────┬──────────┘
                       │
              ┌────────▼─────────┐
              │  Episodic Memory  │  (Stage 12 — post-mortem write/read)
              │  (Vector DB, RRF, │
              │   reranking)      │
              └───────────────────┘
```

Supporting infrastructure introduced in later phases: MCP servers for
tool access (Stage 13–14), Slack/wiki integration (Stage 15), sandboxed
execution (Stage 16), Temporal for durable workflows (Stage 17),
human-in-the-loop approval gates (Stage 18), and an eval/observability
layer wrapping the whole system (Stage 19–21).

## 4. Key design decisions

- **Spec-driven development via OpenSpec** — see
  `comparison-bmad-speckit-openspec.md` for rationale.
- **LangGraph** for multi-agent state management (Stage 6+), chosen over a
  bespoke orchestration loop for explicit state graphs and checkpointing.
- **Pydantic + JSON mode/grammars** for all inter-agent and tool-call
  payloads (Stage 4) — no free-text parsing between agents.
- **MCP (Model Context Protocol)** as the sole tool-access layer (Stage
  13+), so every tool integration is uniform and discoverable.
- **Temporal** for the incident workflow (Stage 17), so long-running
  incidents survive process restarts.
- **Human-in-the-loop by default for anything above "low risk"** — see
  `risk-classification.md`. This is a hard constraint on every later
  stage, not a Stage 18 add-on.

## 5. Acceptance criteria for this stage

- [x] `openspec/proposal.md` exists and describes scope + target
      architecture
- [x] `openspec/agent-responsibility-matrix.md` exists, covering every
      agent introduced through Phase C
- [x] `openspec/risk-classification.md` exists with a concrete low /
      medium / high taxonomy of agent actions
- [x] `openspec/comparison-bmad-speckit-openspec.md` exists
- [x] Proposal is internally consistent with `ROADMAP.md`

## 6. Risks / open questions carried into later stages

- Exact LangGraph state schema is deferred to Stage 6 (needs Stage 4's
  Pydantic schemas first).
- Which specific vector DB deployment mode (local vs. hosted Qdrant) is
  deferred to Stage 10.
- Whether the debate mechanism (Stage 8) needs a third "judge" agent or
  converges via the Incident Commander is deferred to Stage 8 design.
