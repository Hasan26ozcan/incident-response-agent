# Incident Response Agent

An agentic AI system that triages, diagnoses, and (eventually) helps remediate
production incidents — built as a 23-stage, portfolio-grade project spanning
single-agent basics, multi-agent orchestration, retrieval/memory,
tool integration (MCP), durable workflows (Temporal), human-in-the-loop
approval, and full evaluation/observability.

This repo is deliberately built **stage by stage**, with each stage producing
a demonstrable artifact, so the project has portfolio value even if later
stages are cut short.

## Status

🚧 **Stage 0 — Spec & Architecture** (this commit)

See [`ROADMAP.md`](./ROADMAP.md) for the full 23-stage plan across 8 phases,
and [`openspec/`](./openspec) for the formal spec, proposal, and design
decisions behind this stage.

## Why OpenSpec

This project uses [OpenSpec](https://github.com/Fission-AI/OpenSpec)-style
spec-driven development: every non-trivial change starts as a **proposal**
in `openspec/`, gets reviewed/approved, and only then gets implemented.
See `openspec/comparison-bmad-speckit-openspec.md` for why this was chosen
over BMAD-METHOD and GitHub Spec Kit.

## Project layout

```
incident-response-agent/
├── README.md
├── ROADMAP.md
├── LICENSE
├── .gitignore
└── openspec/
    ├── README.md
    ├── proposal.md                          # Stage 0 proposal (scope, architecture)
    ├── agent-responsibility-matrix.md       # Who does what, and who may act
    ├── risk-classification.md               # Low / medium / high risk actions
    └── comparison-bmad-speckit-openspec.md  # BMAD vs Spec Kit vs OpenSpec
```

Future stages will add `src/`, `tests/`, `agents/`, `mcp_servers/`, etc. —
those are intentionally not created yet, per the OpenSpec proposal, so that
Stage 2 (repo skeleton & CI) can lay them down against a green CI baseline
rather than retrofitting structure onto ad-hoc code.

## Getting started (once Stage 2 lands)

Nothing to run yet — Stage 0 is spec-only. Stage 2 will add dependency
management, CI (ruff / bandit / mypy), and a sandboxed execution environment.

## License

MIT — see [`LICENSE`](./LICENSE).
