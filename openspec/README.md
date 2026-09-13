# OpenSpec

This directory is the source of truth for **what** is being built and
**why**, kept separate from **how** it's implemented (which lives in code).

## Workflow

1. **Propose** — a change (usually one roadmap stage, sometimes a slice of
   one) is written up as a proposal: scope, rationale, affected components,
   risk classification, acceptance criteria.
2. **Review / approve** — the proposal is checked against the risk
   classification and agent responsibility matrix before any code is
   written. High-risk changes (see `risk-classification.md`) get extra
   scrutiny here.
3. **Implement** — only after approval. Implementation must satisfy the
   proposal's acceptance criteria; if reality diverges from the plan, the
   proposal is amended first.
4. **Archive** — once shipped, the proposal is marked complete and kept as
   a historical record of a decision, not deleted.

## Files in this stage

| File | Purpose |
|---|---|
| `proposal.md` | Stage 0 proposal: scope, architecture overview, acceptance criteria |
| `agent-responsibility-matrix.md` | Which agent owns which responsibility, and what it's allowed to touch |
| `risk-classification.md` | Low / medium / high risk action taxonomy used throughout the project |
| `comparison-bmad-speckit-openspec.md` | Why OpenSpec was chosen over BMAD-METHOD and GitHub Spec Kit |

Later stages will each get their own `changes/NNN-stage-name/proposal.md`
under this directory once Stage 1 begins.
