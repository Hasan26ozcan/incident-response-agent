# 022 - Notification and reporting (Stage 15)

## Why
Roadmap Stage 15: a diagnosis should end as a published post-mortem page and a Slack notification. Both are externally visible writes (risk: medium, `openspec/risk-classification.md`).

## What
- `reporting/`: deterministic page, Slack message, file wiki, Confluence client, HTTP client, sanitising, `Reporter` with a sent-once ledger, CLI and `diagnose --report`.

## Design decisions
- The page is a template over `Diagnosis` fields. An LLM-written post-mortem could claim impact or lessons nobody verified; this one lists them as follow-ups for a human.
- All free text comes from logs and is untrusted: redacted, control characters removed, Markdown and Slack structure escaped, code spans for evidence.
- The shown risk tier is raised to the `risk_floor` of the recommendation, never lowered.
- Live delivery is opt-in (`--live`); the environment alone never causes an external write. Webhook host is restricted to hooks.slack.com; redirects are never followed; errors never contain URLs, headers or tokens.
- Notification must not break a diagnosis: errors are returned, not raised. Wiki and Slack fail independently. A lost alert is worse than a duplicate, so a corrupt ledger line is ignored.
- Idempotent: same content, same page version, one Slack message. Changed content or a newly available link is announced again.

## Out of scope
Human approval before sending (Stage 18), real-service verification, Slack threads/updates, other wiki products, rendering fidelity in Confluence.

## Acceptance
Unit and real-HTTP tests, all 28 incidents offline, mutation check, coverage, demo.
