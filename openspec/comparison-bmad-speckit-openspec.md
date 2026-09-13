# BMAD-METHOD vs. GitHub Spec Kit vs. OpenSpec

Short comparison note — written for interview prep as much as for the
project decision record.

## BMAD-METHOD

Role-based agentic planning framework: a fixed cast of persona agents
(Analyst, PM, Architect, Scrum Master, Dev, QA) collaborate through
structured markdown documents (PRD, architecture doc, story files) across
multiple sessions. Strong for long-horizon, multi-session projects where
you want the "team" ceremony explicitly modeled — each persona has a
distinct voice and hands off a specific artifact to the next. Heavier
process overhead; well suited to greenfield product work where the
requirements themselves need to be discovered through that role-play.

## GitHub Spec Kit

Lightweight, CLI-driven spec workflow: `spec.md` → `plan.md` → `tasks.md`,
designed to slot into an existing repo and pair naturally with
Copilot/Claude Code style coding agents. Minimal ceremony, fast to adopt,
git-native. Weaker at modeling *why* a decision was made once several
<<<<<<< HEAD
specs have accumulated — it's oriented around a single feature's lifecycle
=======
specs accumulate — it's oriented around a single feature's lifecycle
>>>>>>> 60eb90cfe3ed360fa4b33aae3322fe3b15f103ef
rather than a long-running log of changes and approvals.

## OpenSpec

Change-proposal-centric: specs are the source of truth, and every
modification is an explicit, reviewable **proposal** (`openspec/changes/…`)
that must be approved before implementation and is archived afterward.
This produces a durable, diffable history of *what changed and why* —
<<<<<<< HEAD
closer to an ADR (Architecture Decision Record) log cross-referenced with a living spec.
=======
closer to an ADR (Architecture Decision Record) log crossed with a spec.
>>>>>>> 60eb90cfe3ed360fa4b33aae3322fe3b15f103ef

## Why OpenSpec for this project

This project is a single long-running repo built incrementally across 23
discrete, independently-valuable stages. That maps almost exactly onto
OpenSpec's "one proposal per change" model: each stage *is* a proposal,
with its own scope, risk classification, and acceptance criteria, and the
approval gate naturally enforces the "prove one thing per stage"
discipline the roadmap is built around. BMAD's role-play ceremony is
more than this project needs (there's one builder, not a simulated team),
and Spec Kit's single-feature orientation would require bolting on the
same "history of changes" tracking that OpenSpec gives for free.

**Interview-ready one-liner:** *BMAD role-plays a team to discover
requirements; Spec Kit is a fast spec→plan→tasks loop for one feature at a
time; OpenSpec treats every change as a reviewable proposal against a
living spec — which is what a 23-stage incremental build needs most.*
