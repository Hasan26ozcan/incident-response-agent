# ruff: noqa: E501
"""Append the Stage 21d changelog entry and checkpoint row (idempotent)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = """
## [Stage 21d] — Real-model measurement tooling — 2026-11-22
### Added
- `src/incident_agent/measure/`: model specs (`provider/model`, `cascade:cheap>strong`), campaigns, resumable runner,
  report with bootstrap intervals, paired comparison and measured cascade saving; `python -m incident_agent.measure plan|run|report`.
- `docs/STAGE21D_MEASUREMENT.md`, `openspec/changes/031-measurement/`, `tests/test_measure.py` (fake providers).
### Known limits
- No real-provider numbers yet: they are produced when the owner runs the campaigns with a local .env.
"""
ROW = "| 24 | 2026-11-22 | Stage 21d real-model measurement tooling | stage21d-patch.zip | 27 tests, 99% coverage, mutants killed |\n"
log = ROOT / "CHANGELOG.md"
if log.exists() and "[Stage 21d]" not in log.read_text(encoding="utf-8"):
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + "\n" + ENTRY, encoding="utf-8")
    print("updated CHANGELOG.md")
cp = ROOT / "docs" / "CHECKPOINTS.md"
if cp.exists() and "Stage 21d" not in cp.read_text(encoding="utf-8"):
    cp.write_text(cp.read_text(encoding="utf-8").rstrip("\n") + "\n" + ROW, encoding="utf-8")
    print("updated docs/CHECKPOINTS.md")
