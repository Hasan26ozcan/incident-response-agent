# Agent Responsibility Matrix

Defines, for each agent introduced across the roadmap, its responsibility,
what it may read, what it may write/act on, and its default risk ceiling
(see `risk-classification.md` for the tiers themselves). An agent may never
perform an action above its risk ceiling without explicit human approval,
regardless of what a plan or another agent asks it to do.

| Agent | Introduced | Responsibility | Reads | Writes / Acts on | Default risk ceiling |
|---|---|---|---|---|---|
| **Single Agent (ReAct)** | Stage 3 | End-to-end diagnosis in a single loop, before the system is decomposed | Synthetic logs | Diagnosis output only | Low |
| **Triage Agent** | Stage 6 | Classifies incoming incident, decides which specialist workers to invoke | Incident metadata, alert payload | Dispatch instructions to workers | Low |
| **Log Agent** | Stage 6 | Parses and summarizes log data for anomalies | Log store | Structured findings object | Low |
| **Metrics Agent** | Stage 6 | Analyzes metric time series for anomalies/correlations | Metrics store | Structured findings object | Low |
| **Deploy-History Agent** | Stage 6 | Correlates incident timing with recent deploys/config changes | Deploy history store | Structured findings object | Low |
| **Incident Commander** | Stage 7 | Synthesizes worker findings into a unified incident narrative | All worker outputs | Incident summary, escalation decision | Low |
| **Root-Cause Agent** | Stage 8 | Proposes a root-cause hypothesis | All findings, episodic memory | Hypothesis + confidence | Low |
| **Forensic-Examiner Agent** | Stage 8 | Adversarially challenges the root-cause hypothesis (debate) | Same as Root-Cause Agent | Critique + counter-hypothesis | Low |
| **Hypothesis-Branching Agent** | Stage 9 | Explores multiple root-cause branches in parallel (ToT / Plan-and-Solve) | All findings, episodic memory | Ranked hypothesis set | Low |
| **Memory/Reflection Agent** | Stage 12 | Writes post-mortems, embeds them, flags past misdiagnoses, updates strategy notes | Resolved incidents, past post-mortems | Vector DB (episodic memory) | Medium (writes persistent memory) |
| **Notification Agent** | Stage 15 | Sends Slack notifications, publishes post-mortem to wiki | Incident summary, post-mortem | Slack webhook, wiki page (external, visible) | Medium |
| **Remediation Agent** | Stage 16+ | Proposes and, on approval, executes remediation scripts/commands | Root-cause output, runbook library | Sandboxed shell/script execution | High (always gated) |
| **Escalation/HITL Gate** | Stage 18 | Enforces human approval before any medium/high-risk action proceeds; escalates on timeout | Pending action queue | Approval state, escalation notifications | N/A (control component, not a domain agent) |

## Cross-cutting rules

1. **No agent bypasses the HITL gate.** The gate is infrastructure, not a
   suggestion any agent can route around — it is enforced at the tool-call
   layer (via MCP), not by prompting.
2. **Read scope is always narrower than write scope suggests.** E.g. the
   Root-Cause Agent reads episodic memory but never writes to it directly;
   only the Memory/Reflection Agent writes, so memory updates go through
   one auditable path.
3. **Every agent's output is a validated Pydantic object** (Stage 4
   onward) — no agent passes free text to another agent or to a tool.
4. **New agents added after Stage 9** (e.g. any Stage 13+ tool-specific
   agent) must be added to this table before implementation, per the
   OpenSpec workflow.
