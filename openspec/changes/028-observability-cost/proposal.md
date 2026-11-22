# Proposal: Stage 21 — Observability & Cost

- **Status:** Implemented
- **Stage:** 21 of 23
- **Phase:** G — Evaluation & Security
- **Depends on:** Stages 3 (LLM layer), 17 (journals), 19 (eval gate)

## 1. Problem

Nothing showed what a run cost, how long each step took, how much of a prompt a provider could cache, or what the
agent did in order. Context could grow without bound and every call used the same model.

## 2. Decisions

1. **Local-first tracing**; LangSmith / Phoenix / W&B are optional adapters, never required.
2. **Telemetry never changes behaviour**: sink failure is counted, not raised; wrappers pass calls through unchanged.
3. **No secrets, no content by default**: redaction on every attribute and error; message text only on opt-in.
4. **Verified prices only**; unknown = unknown.
5. **Cascade with an explicit quality gate**, escalating on rejection/unavailability, never hiding config errors.
6. **Windowing that cannot orphan a tool message.**
7. **Gate it**: an `observability` section with negative controls in the Stage 19 suite.
8. **Do not wire in.** Wrappers and tools only; agent/CLI wiring needs the owner's go-ahead.

## 3. Out of scope

Real-provider measurement of cascade quality and latency (needs keys, run by the owner), live exporter verification,
dashboards served over HTTP, wiring into agents/MCP, Stage 22 (docs/demo/release).

## 4. Acceptance

`python -m incident_agent.agenteval run` passes with the `observability` section; `tests/test_observe.py` passes;
`python -m incident_agent.observe demo` writes the report and dashboard.
