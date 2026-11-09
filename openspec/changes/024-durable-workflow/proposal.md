# Proposal: Stage 17 — Durable (Temporal) Workflow

- **Status:** Implemented. Core fully verified; the Temporal server path is verified statically only in the build sandbox.
- **Stage:** 17 of 23
- **Phase:** F — Orchestration & Resilience
- **Depends on:** Stage 16 (sandbox runner)

## 1. Problem

An incident workflow (triage → diagnose → plan → approval wait → execute → report) can run for hours because it waits
for a human. A crash, deploy or reboot in the middle must not lose progress, repeat finished work, or run a mutating
command twice.

## 2. Scope

In scope: an event-sourced durable engine with no external dependency, the incident workflow built on it, a CLI with a
kill-and-resume demo, and a thin Temporal adapter that runs the *same* step functions.
Out of scope: the human approval UX and escalation (Stage 18); wiring into agents or MCP servers (needs a go-ahead).

## 3. Design decisions

1. **Two layers.** Layer 1 (`journal.py`, `engine.py`, `workflows.py`) needs only the standard library and is tested
   here, including a real `SIGKILL` and resume. Layer 2 (`temporal.py`) only orders the same steps; one activity
   (`incident_step`) calls the shared step functions.
2. **Event sourcing.** State is a fold of an append-only, fsynced JSONL journal; a torn last line is ignored and
   truncated; corruption in the middle refuses to load.
3. **At-least-once steps, at-most-once `execute`.** Steps get a stable idempotency key; `execute` has `max_attempts=1`,
   attempts are counted across crashes, and its approval nonce is deterministic so a replay cannot spend it twice.
4. **Durable waits.** The deadline and the approval signal are journalled; a restart does not extend a wait.
5. **One worker per run.** OS file lock, released automatically when the owner dies (also on SIGKILL).
6. **Definition guard.** Resuming with a different workflow name, version or step list is refused.
7. **Only a clear, named approval applies a change.** Rejection, timeout, a missing approver or `approved != true`
   only produce a `--dry-run`. A weak sandbox backend still refuses mutating commands.

## 4. Acceptance criteria (ROADMAP: "post-interruption resume demo")

- `python -m incident_agent.durable demo` kills the worker with SIGKILL after `diagnose`, resumes, waits for approval,
  applies it, and shows every step ran exactly once.
- An interrupted step re-runs with `interrupted_before=True`; completed steps are never re-run.
- `execute` runs at most once; a crash inside it fails the run for a human to look at.
- Tests: `tests/test_durable.py`, `tests/test_temporal_adapter.py` (server tests skip without a Temporal server).

## 5. Known limits

- A step timeout abandons a thread, it cannot kill it. The Windows lock/kill path is untested here.
- The `process` sandbox backend refuses mutating commands: use `namespace` or `docker` for a real `execute`.
- The Temporal workflow body is only exercised by server-backed tests, which need a local Temporal dev server.
