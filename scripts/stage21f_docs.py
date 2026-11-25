# ruff: noqa: E501
"""Append the Stage 21f changelog entry and checkpoint row (idempotent)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = """
## [Stage 21f] — Service layer — 2026-11-23
### Added
- `src/incident_agent/service/`: HTTP API (submit, runs, approvals, health, ready, metrics), scoped tokens, throttling, Slack notice, dashboard, CLI.
- `deploy/Dockerfile`, `deploy/docker-compose.yml`, `tests/test_service.py`, `docs/STAGE21F_SERVICE.md`, `openspec/changes/033-service/`.
"""
ROW = "| 26 | 2026-11-23 | Stage 21f service layer (API, tokens, health, Slack, dashboard, Docker) | stage21f-patch.zip | 33 service tests over a real socket |\n"
log = ROOT / "CHANGELOG.md"
if log.exists() and "[Stage 21f]" not in log.read_text(encoding="utf-8"):
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + "\n" + ENTRY, encoding="utf-8")
    print("updated CHANGELOG.md")
cp = ROOT / "docs" / "CHECKPOINTS.md"
if cp.exists() and "Stage 21f" not in cp.read_text(encoding="utf-8"):
    cp.write_text(cp.read_text(encoding="utf-8").rstrip("\n") + "\n" + ROW, encoding="utf-8")
    print("updated docs/CHECKPOINTS.md")
