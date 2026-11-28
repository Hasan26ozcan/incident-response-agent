# ruff: noqa: E501
"""Append the Stage 21h changelog entry and checkpoint row (idempotent)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = """
## [Stage 21h] — Security and quality audit — 2026-11-25
### Added
- mypy strict (3 platforms), ruff S/B/PLE/SIM, project semgrep rules, detect-secrets baseline, `.github/workflows/security.yml`, `sonar-project.properties`, `docs/STAGE21H_AUDIT.md`.
### Fixed
- `--agent swarm --report` crashed the post-mortem; sandbox approval-binding asserts now fail closed; `gate` split (complexity F -> B); `ResourceCache.__contains__`.
"""
ROW = "| 28 | 2026-11-25 | Stage 21h security and quality audit | stage21h-patch.zip | mypy strict 0, bandit 0, semgrep 0, pip-audit 0 |\n"
log = ROOT / "CHANGELOG.md"
if log.exists() and "[Stage 21h]" not in log.read_text(encoding="utf-8"):
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + "\n" + ENTRY, encoding="utf-8")
    print("updated CHANGELOG.md")
cp = ROOT / "docs" / "CHECKPOINTS.md"
if cp.exists() and "Stage 21h" not in cp.read_text(encoding="utf-8"):
    cp.write_text(cp.read_text(encoding="utf-8").rstrip("\n") + "\n" + ROW, encoding="utf-8")
    print("updated docs/CHECKPOINTS.md")
