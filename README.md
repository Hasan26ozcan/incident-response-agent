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

✅ **Stage 1 — Synthetic Incident Dataset** (complete)

- 20 synthetic incident scenarios across 19 categories
- Gold answers, rubric, and validation tooling complete
- **269 tests passing** across all stages — dataset generation, validation, integrity, leakage prevention, false-alarm handling, schema validation, prompt library, and more
- Test environment verified: PyTorch 2.14.0+cpu, NumPy 2.4.6

✅ **Stage 2 — Repo Skeleton & CI Baseline** (complete)

See [`ROADMAP.md`](./ROADMAP.md) for the full 23-stage plan across 8 phases,
and [`openspec/`](./openspec) for the formal spec, proposal, and design
decisions behind this stage.

✅ **Stage 3 — Single-Agent ReAct Loop** (complete)

- ReActAgent with observe/reason/act/repeat loop
- Tool functions for logs, metrics, deploys, metadata
- CLI `diagnose` subcommand
- **220 tests passing**

✅ **Stage 5 — Dynamic Planning & Error Recovery** (complete)

- Plan-based execution with ordered diagnostic steps
- Replanning loop rebuilds the plan when a step fails
- `Plan`, `PlanStep`, `StepFailure` data structures
- `generate_plan()` supports standard, log_only, metrics_only strategies
- **304 tests passing** (35 Stage 5 + 269 existing)

✅ **Stage 4 — Structured Output & Prompt Engineering** (complete)

- All agent outputs validated via Pydantic schemas (`Diagnosis`, `EvidenceItem`, `ReasoningStep`, `IncidentMetadata`, `MetricAnomaly`, `AgentOutput`, `RiskTier`)
- JSON-mode compatible serialization (`to_json()` / `from_json()`)
- Prompt library with system prompt template, three few-shot examples, and `build_prompt()` utility
- Cross-cutting rule enforced: every agent output is a validated Pydantic object
- **82 tests passing** (49 Stage 4 + 28 Stage 3 + 5 smoke)
- See [`openspec/changes/004-structured-output-prompt-engineering/proposal.md`](./openspec/changes/004-structured-output-prompt-engineering/proposal.md) for the proposal

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
├── CHANGELOG.md
├── LICENSE
├── .gitignore
├── requirements.txt              # torch, numpy
├── .venv/                        # Python environment (PyTorch 2.14.0+cpu)
├── tests/                        # Test suite
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_torch_env.py         # torch/numpy environment verification
│   └── test_dataset.py           # dataset integrity, validation, invariants
├── data/
│   ├── generate_dataset.py       # Deterministic dataset generator
│   └── incidents/                # 20 incident scenarios (INC-001…INC-020)
│       └── <id>/
│           ├── meta.json
│           ├── logs.log
│           ├── metrics.json
│           └── deploys.json
├── eval/
│   ├── validate_dataset.py       # Mechanical integrity/leakage validator
│   ├── generate_gold_set_md.py   # Regenerates gold_set.md
│   ├── rubric.md                 # Scoring rubric with hard gates
│   ├── gold_set.md               # Human-readable summary table
│   └── gold/                     # 20 gold answers (kept separate from data/)
│       └── INC-0NN.json
└── openspec/
    ├── README.md
    ├── proposal.md                          # Stage 0 proposal (scope, architecture)
    ├── agent-responsibility-matrix.md       # Who does what, and who may act
    ├── risk-classification.md               # Low / medium / high risk actions
    └── comparison-bmad-speckit-openspec.md  # BMAD vs Spec Kit vs OpenSpec
```

Stage 2 already added `src/`, `agents/`, `mcp_servers/` (empty), and CI configuration
on top of this foundation. Stage 4 added `schemas/` (Pydantic models) and `prompts/`
(prompt library with few-shot examples). Stage 5 added `workflows/plan.py` (Plan,
PlanStep, StepFailure) and updated `ReActAgent` with plan-based execution and replanning.

## Getting started

### Prerequisites
- Python 3.11+
- pip

### Setup
```bash
python -m venv .venv
.\.venv\Scripts\activate          # Windows
# source .venv/bin/activate       # macOS/Linux
pip install -r requirements.txt
```

### Regenerate the dataset
```bash
python data/generate_dataset.py   # Generate all 20 incidents + gold answers
python eval/validate_dataset.py   # Validate integrity (no leakage, correct counts)
python eval/generate_gold_set_md.py  # Regenerate gold_set.md
```

### Run tests
```bash
python -m pytest tests/ -v        # 269 tests — all should pass
```

### Verify the torch environment
```bash
python -m pytest tests/test_torch_env.py -v
```

## License

MIT — see [`LICENSE`](./LICENSE).
