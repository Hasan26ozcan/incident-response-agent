# Proposal: Stage 18 — Human-in-the-Loop & Escalation

- **Status:** Implemented
- **Stage:** 18 of 23
- **Phase:** F — Orchestration & Resilience
- **Depends on:** Stage 16 (approvals bound to a fingerprint), Stage 17 (durable `approval` wait)

## 1. Problem

`openspec/risk-classification.md` says high-risk actions must pass a human gate that (a) blocks execution until an
explicit approval, (b) escalates to a human/on-call channel on timeout and (c) never auto-approves on timeout.
Stage 16 verifies an approval; Stage 17 waits for one. Nothing yet decides *who* may approve, *how* people are asked,
or *what happens when nobody answers*.

## 2. Scope

In scope: approval requests as an event-sourced store, an escalation policy (levels, timeouts, quorum per risk tier),
an approver roster with token authentication, notifications (local outbox; Slack through the Stage 15 sender), a
controller that connects the gate to Stage 17 runs without changing the workflow, a CLI and a two-scenario demo.
Out of scope: single sign-on / real identity binding, an approval web UI, paging systems other than Slack, wiring into
agents (needs a go-ahead), Stage 19+ evaluation.

## 3. Design decisions

1. **Timeout escalates, silence is a refusal.** The last level running out *expires* the request; expired maps to
   `approved: false`, which the Stage 17 workflow turns into a `--dry-run`. No code path creates an approval from time.
2. **The gate never executes anything.** It produces one `approval` signal per run; the workflow and the Stage 16
   runner stay the only code that can run a command, and they re-check the fingerprint, expiry and one-time nonce.
3. **Event-sourced, crash-safe.** One journal per request (the Stage 17 journal). State is a fold; escalation is
   recomputed from stored deadlines, so a controller that was down for hours catches up level by level.
4. **Authenticated, eligible, distinct approvers.** A token per approver (only a SHA-256 digest is stored, tokens come
   from the environment, never from arguments). Roles asked so far define who may answer; earlier levels keep their
   right after an escalation. Duplicates do not count twice; the requester cannot decide their own request.
5. **Approvers must read what they approve.** An approval carries the first 8 characters of the action's fingerprint.
6. **Late answers lose.** Timeouts are applied before every decision, so an approval after expiry cannot revive it.
7. **Deadline = the run's own wait deadline.** The request is capped by the durable `approval` wait, so the gate and the
   run cannot disagree about when waiting ends.
8. **Notifications cannot lose or decide a request.** Failures are journalled and retried a bounded number of times;
   messages are redacted and escaped like the Stage 15 reports.
9. **Quorum by tier.** `required` maps a risk tier to 1-3 approvals (never 0); the default for `high` is 1 and is
   configurable (for example 2 for four-eyes).

## 4. Acceptance criteria (ROADMAP: "test scenario covering pending approval and timeout escalation")

- A pending request stays pending, escalates level by level on timeout, notifies each level and finally expires
  without ever approving (`test_pending_approval_then_timeout_escalation_never_approves`).
- The three high-risk rules of `risk-classification.md` hold (`test_the_gate_matches_the_high_risk_rules_in_the_spec`)
  and a randomised history test never finds an approval without enough valid, distinct, eligible approvers.
- End to end with a durable run: approved → applied once; rejected, expired or cancelled → dry run / nothing executed.
- `python -m incident_agent.hitl demo` shows both scenarios with real timing.

## 5. Known limits

- Authentication is possession of a local token. Whoever holds a token is that person; whoever can read the roster file
  cannot approve, but a person who shares a token defeats the gate. Real identity (SSO, signed requests) is future work.
- A controller or CLI process that can run `approve` with a valid token can approve; the gate is not a boundary against
  an agent that has been given a token. Do not give agents tokens.
- Notifications are at-least-once up to 5 attempts per level; after that the state shows the failure and a human has to
  look (`list`/`show`). Live Slack delivery is only exercised against fakes here.
- Escalation time is wall-clock time on the controller's machine; there is no clock synchronisation across hosts.
