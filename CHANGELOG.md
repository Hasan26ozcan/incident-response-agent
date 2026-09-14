# Changelog

All notable changes to the Incident Response Agent project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Stage 1] — 2026-09-14
### Added
- 20 synthetic incident scenarios (`data/incidents/INC-001`…`INC-020`) covering 19 categories
- Each scenario has `meta.json`, `logs.log`, `metrics.json`, `deploys.json`
- 20 gold answers (`eval/gold/INC-001.json`…`INC-020.json`) with root cause, evidence, remediation, and risk tier
- `eval/rubric.md` — composite scoring rubric with two hard gates (false-positive handling, safety/risk-tier compliance)
- `eval/gold_set.md` — mechanically generated human-readable summary table
- `data/generate_dataset.py` — deterministic, stdlib-only dataset generator
- `eval/validate_dataset.py` — mechanical integrity/leakage validator
- `eval/generate_gold_set_md.py` — regenerates `gold_set.md` from gold JSON files
- Full test suite (`tests/`) with 187 tests covering generator, validator, integrity, leakage, false-alarm, recurrence, and distractor invariants
- `requirements.txt` — project dependencies (torch, numpy)
- `tests/test_torch_env.py` — verifies torch/numpy environment
- `tests/test_dataset.py` — comprehensive dataset tests
- `.venv/` Python environment with PyTorch 2.14.0+cpu and NumPy 2.4.6
### Changed
- `.gitignore` — fixed merge conflict markers, added eval/data artifact paths
### Verified
- All 187 tests pass (`python -m pytest tests/ -v`)
- `data/generate_dataset.py` runs cleanly and is deterministic across runs
- `eval/validate_dataset.py` passes with no problems found
- PyTorch 2.14.0+cpu and NumPy 2.4.6 available and interoperable
- Gold answers kept structurally separate from agent-visible data (no eval leakage)

## [Unreleased]

### Added
- Project skeleton: README, ROADMAP, LICENSE, .gitignore
- OpenSpec spec suite: proposal, agent responsibility matrix, risk classification, methodology comparison
- 23-stage roadmap across 8 phases (Phase A–H)

## [Stage 0] — 2026-09-13
### Added
- `openspec/proposal.md` — Scope, architecture overview, design decisions, acceptance criteria
- `openspec/agent-responsibility-matrix.md` — Per-agent responsibility, read/write scope, risk ceiling
- `openspec/risk-classification.md` — Low / medium / high risk taxonomy with governance rules
- `openspec/comparison-bmad-speckit-openspec.md` — Methodology comparison and rationale for choosing OpenSpec
- `openspec/README.md` — OpenSpec workflow documentation
- `CHANGELOG.md` — This file

---

## Template for future stages

Each stage adds its own section here following this pattern:

```markdown
## [Stage N] — <Title> — <YYYY-MM-DD>
### Added
- What was delivered
### Changed
- What was modified from prior stages
### Verified
- Acceptance criteria met
```
