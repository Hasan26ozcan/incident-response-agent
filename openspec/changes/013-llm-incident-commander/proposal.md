# Proposal: Stage 7 (rewrite) — LLM Incident Commander over the Orchestrator

- **Status:** Implemented, awaiting measurement
- **Stage:** 7 of 23 — Phase C, Hierarchical Swarm
- **Depends on:** change 012 (LLM orchestrator-workers)
- **Roadmap contract:** "Incident Commander agent added; synthesizes workers' findings. Output: end-to-end
  diagnosis flow (no actions yet)."

## 1. Why a rewrite

The original `IncidentCommander` (change 007) is a string template: its narrative restates the
orchestrator's root cause, its recommendations contain invented procedure ("check back in 15 minutes",
"resolution through automated remediation"), confidence is inflated by +0.05, and the category comes from
`meta["category"]`, which does not exist. Its 61 tests mostly assert types ("is a string", "is a valid
model"), so they could not detect any of this. It remains for backward compatibility (Stage 8+ import it)
but is not part of the measured system.

## 2. Design

```
HierarchicalSwarm.run(incident)
  └─ LLMOrchestrator (change 012): triage → 3 workers → synthesis ⇒ Diagnosis + worker reports
  └─ LLMIncidentCommander (no tools)
       1. compute_signals()   code: coverage, domains normal/anomalous, verified-fact ratio, onset spread
       2. review (1 LLM call) narrative, agree/revise, severity SEV1-4, open questions
       3. revision gate       a revision is applied only if its supporting quotes are found in tool output
       4. decide_escalation() deterministic policy; the model never decides who is paged
       ⇒ CommanderDiagnosis (existing schema) with the final Diagnosis embedded
```

Decisions:
- **Escalation is deterministic** (first rule wins): degraded run → escalate; HIGH risk → escalate (human
  approval); confidence below threshold → escalate; false alarm → resolve only if every domain confirms
  *and* risk is LOW, else monitor; MEDIUM → monitor; incomplete coverage → monitor; else resolve. Invariant,
  tested over the whole input space: HIGH risk, a degraded run or low confidence can never end in
  monitor/resolve, and `resolve` implies LOW risk, full coverage and sufficient confidence.
- **Revision is evidence-gated.** The commander can change the diagnosis only with verbatim quotes that exist
  in the workers' tool output (≥ 50 % grounded). Otherwise the orchestrator's answer stands and the
  rejection is recorded. `llm_swarm_norevise` disables revisions for the ablation.
- **Narrative is checked mechanically**: clock times in it must occur in the tool output, else −0.05
  confidence each (cap −0.15) and a note.
- **Confidence is never inflated**: −0.1 per failed worker; grounding penalties carry through.
- **Failure handling**: commander outage/invalid output → template narrative + rule-based severity, the
  diagnosis is kept; orchestrator degraded → commander model not called, escalate; config errors surface.
- **No actions.** The commander is never offered a tool; the recommendation says remediation is "not
  executed". Execution is Stage 16+, behind the Stage 18 approval gate.

## 3. Acceptance criteria

- [x] End-to-end flow returns a validated `CommanderDiagnosis` (JSON round-trip)
- [x] Synthesises all workers' findings; hierarchy order triage → workers → synthesis → commander
- [x] Commander has no tools; no remediation is executed (tests)
- [x] Escalation policy: every rule, plus safety invariants over the full input space
- [x] Revision accepted/rejected/disabled paths; false-alarm and risk-tier revisions
- [x] Orchestrator degradation, worker failure, commander outage, garbage output, config error
- [x] Harness: `--agent llm_swarm` and `--agent llm_swarm_norevise` with commander trace
- [x] 16/16 deliberate mutations of the policy, gating, scope and guard logic are caught by the tests
- [ ] **Measured** on the dev split (below)

## 4. Ablation protocol (dev split; test split only at the milestone)

Same model, `--temperature 0`, one tag each:
1. `llm_orchestrator`            — Stage 6
2. `llm_swarm_norevise`          — commander adds narrative, severity, escalation only
3. `llm_swarm`                   — commander may also revise the diagnosis

Report root_cause_f1, evidence_recall, risk_accuracy, false_alarm_accuracy, crashed/degraded, calls and
tokens, plus per-incident revisions (`trace.commander.revision_applied`) and whether each revision helped
or hurt. The commander adds one call per incident; if revisions do not help the honest conclusion is to
keep it as narrative/escalation layer only.
Escalation has no gold label; inspect the distribution in traces instead of scoring it.

## 5. Risks

- A commander that "revises" to a worse answer. Mitigation: evidence gate, `norevise` ablation, per-incident review.
- Narrative time check catches invented clock times, not invented prose. Narrative quality is reviewed by reading.
