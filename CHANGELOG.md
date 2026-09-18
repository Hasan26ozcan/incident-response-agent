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

## [Stage 4] — 2026-09-18
### Added
- `src/incident_agent/schemas/` package with Pydantic models:
  - `Diagnosis` — replaces `dataclasses.Diagnosis` with full schema validation
  - `EvidenceItem`, `ReasoningStep`, `IncidentMetadata`, `MetricAnomaly`, `AgentOutput`, `RiskTier`
- `src/incident_agent/prompts/` package with prompt library:
  - `system_prompt.py` — system prompt template with role, constraints, and output schema
  - `few_shot.py` — three curated few-shot examples covering diverse categories and risk tiers
  - `builder.py` — `build_prompt()` utility to assemble complete prompts
- `ReActAgent` updated to produce Pydantic-validated `Diagnosis` objects
- Cross-cutting rule enforced: every agent output is a validated Pydantic object
- `tests/test_stage4.py` — 49 schema validation and prompt library tests
- OpenSpec proposal `openspec/changes/004-structured-output-prompt-engineering/proposal.md`
- Pydantic added to `pyproject.toml` and `requirements.txt`
### Changed
- `src/incident_agent/agents/react_agent.py` — now produces Pydantic `Diagnosis` instead of `dataclasses.Diagnosis`
- `src/incident_agent/__init__.py` — exports all Pydantic schema types
- `src/incident_agent/agents/__init__.py` — exports Pydantic `Diagnosis`
- `src/incident_agent/schemas/__init__.py` — new canonical schema exports
### Verified
- All 82 tests pass (49 Stage 4 + 28 Stage 3 + 5 smoke)
- Schema validation catches invalid inputs (missing fields, out-of-bounds confidence, empty evidence)
- JSON round-trip (`to_json` → `from_json`) produces valid Diagnosis objects
- Prompt builder produces valid structured prompts with few-shot examples
- Backward compatible: existing `format_report()` and CLI work unchanged

## [Stage 3] — 2026-09-16
### Added
- `src/incident_agent/agents/react_agent.py` — ReActAgent with observe/reason/act/repeat loop
- `src/incident_agent/tools/` — tool functions: read_meta, read_logs, find_error_logs, detect_anomaly_timestamps, get_recent_deploys, find_metric_spikes, read_metrics, read_deploys
- `src/incident_agent/cli.py` — diagnose subcommand
- `Diagnosis` dataclass with structured output and format_report()
- 28 comprehensive Stage 3 tests
### Changed
- README.md — updated Stage 3 status
### Verified
- All 220 tests passing
- ruff, mypy, bandit clean

## [Stage 2] — 2026-09-15
### Added
- Project skeleton: `src/incident_agent/` package with subpackages (`agents/`, `tools/`, `memory/`, `workflows/`, `eval/`)
- `pyproject.toml` with hatchling build, ruff/bandit/mypy config
- CI pipeline (`.github/workflows/ci.yml`): ruff, bandit, mypy, pytest, trivy, dataset regeneration check
- `Makefile` with 10 targets (`make ci`, `make sandbox-build`, `make sandbox-shell`, etc.)
- `.pre-commit-config.yaml` (ruff, pre-commit hooks)
- Docker configuration: `Dockerfile`, `docker-compose.yml`, `docker/sandbox/Dockerfile` (hardened non-root image)
- `docker/sandbox/README.md` — sandbox invocation patterns and rationale
- `src/incident_agent/cli.py` — console script entry point placeholder
- `src/incident_agent/py.typed` — marker file for type-aware imports
- 5 Stage-2 smoke tests (`tests/test_smoke.py`) — package install, CLI, subpackage imports
- **192 tests total, all passing**
### Changed
- `.gitignore` — added data/generated/, eval/results/ paths
- README.md — updated Stage 2 status, corrected test count to 192
### Verified
- All 192 tests pass
- ruff check, ruff format, mypy, bandit — all green
- Docker image builds and runs as non-root user
- Sandbox has no network access by default

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
