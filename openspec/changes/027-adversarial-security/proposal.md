# Proposal: Stage 20 — Adversarial Testing & Security

- **Status:** Implemented
- **Stage:** 20 of 23
- **Phase:** G — Evaluation & Security
- **Depends on:** Stages 16 (sandbox), 17 (durable runs), 18 (approval gate), 19 (eval harness)

## 1. Problem

Stage 19 reported one open hole (an unsealed `approval` signal bypassed the gate) and nothing attacked the system
with hostile content: injected instructions in logs, forged or replayed approvals, hostile identifiers, an
emergency stop that the agent could undo.

## 2. Decisions

1. **Seal approvals.** HMAC over (run, plan fingerprint, decision); verified in `execute`; default fails closed.
2. **Kill-switch is a property of the infrastructure, not the agent**: operator-token release, fail-closed state,
   environment override, enforced in the backend and in every step.
3. **Filter + architecture.** Injection scanning reduces exposure and makes attempts visible; safety rests on the fixed
   command table, sealed approvals and the kill-switch.
4. **Verdicts, not pass/fail.** blocked / exposed / evaded / gap / vulnerable; only `vulnerable` fails CI, and
   exposed/gap/evaded counts may not grow past the baseline.
5. **Do not wire into agents.** The defences are libraries plus tests; wiring needs the owner's go-ahead.

## 3. Out of scope

Store integrity chain / OS-user separation (APP-08), LLM-judge detection (INJ-07), wiring `harden()` into the prompt
builder (INJ-09), observability and cost (Stage 21).

## 4. Acceptance

- `python -m incident_agent.security attacks` exits 0 with 0 `vulnerable`; removing any single defence makes its attack
  report `vulnerable` (tests).
- The Stage 19 gate includes the `security` section and fails on a new vulnerable, exposed, gap or evaded item.
