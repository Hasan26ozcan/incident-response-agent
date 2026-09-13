# Risk Classification of Agent Actions

Every action any agent can take in this system falls into exactly one of
three tiers. This taxonomy is referenced by the agent responsibility
matrix and enforced at the tool-call (MCP) layer from Stage 13 onward —
before that, it's enforced structurally (workers simply have no write
tools).

## Low risk — autonomous, no approval required

Read-only, or writes confined to the agent's own internal reasoning
artifacts (not externally visible, not persistent beyond the current
incident run).

- Reading logs, metrics, deploy history, runbooks
- Querying the vector DB / episodic memory
- Producing structured findings, hypotheses, or summaries returned to
  another agent
- Running diagnostics inside the sandbox that have no side effects
  (e.g. `grep`, read-only queries)

**Governance:** none beyond normal eval/regression testing (Stage 19).

## Medium risk — autonomous, but visible/persistent and logged

Actions that are reversible in principle but create external or
persistent state that a human would notice or rely on.

- Writing a post-mortem to episodic memory (Stage 12)
- Sending a Slack notification (Stage 15)
- Publishing a post-mortem page to a wiki (Stage 15)
- Creating a ticket/ticket comment

**Governance:** allowed to proceed autonomously, but every action is
logged with full provenance (which agent, which incident, which inputs)
and is included in the observability trace (Stage 21). Sampled for
adversarial review (Stage 20).

## High risk — always requires human approval

Actions with real-world side effects on production or production-adjacent
systems, or actions that are hard/impossible to cleanly reverse.

- Executing any remediation script or shell command outside the read-only
  set (Stage 16)
- Restarting, scaling, or redeploying a service
- Rolling back a deploy
- Any action touching credentials, secrets, or access control
- Any action that was not explicitly enumerated in the runbook the agent
  is following

**Governance:** must pass through the Human-in-the-Loop escalation gate
(Stage 18). The gate:

- Blocks execution until an explicit human approval is received, or
- Escalates to a human/on-call channel on timeout, and
- Never allows a default "auto-approve on timeout" — timeout means
  escalate, not proceed.

## Classifying a new action

When a later stage introduces a new capability, classify it here (via an
OpenSpec proposal amendment) *before* implementing it. The default when
in doubt is to classify **up** (treat it as riskier), not down.
