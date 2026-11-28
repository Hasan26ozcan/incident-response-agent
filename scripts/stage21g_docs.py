# ruff: noqa: E501
"""Append the Stage 21g changelog entry and checkpoint row (idempotent)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = """
## [Stage 21g] — Whole-system acceptance — 2026-11-23
### Added
- `python -m incident_agent.acceptance`: 10-step offline chain (lab, pipeline, security, service shadow/assist/restart, measurement, gate, leak scan).
- `tests/test_acceptance.py`, `docs/STAGE21G_ACCEPTANCE.md`, `openspec/changes/034-acceptance/`.
### Fixed
- Service: after a restart, runs waiting for approval can complete; runs cut short are reported `interrupted`.
"""
ROW = "| 27 | 2026-11-23 | Stage 21g whole-system acceptance + service restart recovery | stage21g-patch.zip | acceptance 10/10, clean copy and fresh venv |\n"
log = ROOT / "CHANGELOG.md"
if log.exists() and "[Stage 21g]" not in log.read_text(encoding="utf-8"):
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + "\n" + ENTRY, encoding="utf-8")
    print("updated CHANGELOG.md")
cp = ROOT / "docs" / "CHECKPOINTS.md"
if cp.exists() and "Stage 21g" not in cp.read_text(encoding="utf-8"):
    cp.write_text(cp.read_text(encoding="utf-8").rstrip("\n") + "\n" + ROW, encoding="utf-8")
    print("updated docs/CHECKPOINTS.md")
