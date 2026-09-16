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
- **192 tests passing** — dataset generation, validation, integrity, leakage prevention, false-alarm handling, recurrence pairs, and distractor-pair invariants
- Test environment verified: PyTorch 2.14.0+cpu, NumPy 2.4.6

✅ **Stage 2 — Repo Skeleton & CI Baseline** (complete)

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
├── CHANGELOG.md
├── LICENSE
├── .gitignore
├── requirements.txt              # torch, numpy
├── .venv/                        # Python environment (PyTorch 2.14.0+cpu)
├── tests/                        # Test suite — 192 tests
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
on top of this foundation.

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
python -m pytest tests/ -v        # 192 tests — all should pass
```

### Verify the torch environment
```bash
python -m pytest tests/test_torch_env.py -v
```

## License

MIT — see [`LICENSE`](./LICENSE).
