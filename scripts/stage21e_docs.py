# ruff: noqa: E501
"""Append the Stage 21e changelog entry and checkpoint row (idempotent)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = """
## [Stage 21e] — Security closure — 2026-11-23
### Changed
- Injection hardening is on by default in `build_prompt` and the tool loop (INJ-09 closed).
- `RequestStore(seal_key=...)` chains events with HMAC; tampering raises `StoreTampered` and the run expires closed (APP-08 closed).
### Added
- `tests/test_integrity.py`, `docs/STAGE21E_SECURITY_CLOSURE.md`, `openspec/changes/032-security-closure/`.
"""
ROW = "| 25 | 2026-11-23 | Stage 21e security closure (INJ-09 default, APP-08 store chain) | stage21e-patch.zip | security 31 blocked, 0 gap, 0 vulnerable |\n"
log = ROOT / "CHANGELOG.md"
if log.exists() and "[Stage 21e]" not in log.read_text(encoding="utf-8"):
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + "\n" + ENTRY, encoding="utf-8")
    print("updated CHANGELOG.md")
cp = ROOT / "docs" / "CHECKPOINTS.md"
if cp.exists() and "Stage 21e" not in cp.read_text(encoding="utf-8"):
    cp.write_text(cp.read_text(encoding="utf-8").rstrip("\n") + "\n" + ROW, encoding="utf-8")
    print("updated docs/CHECKPOINTS.md")
