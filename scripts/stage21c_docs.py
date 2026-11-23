# ruff: noqa: E501
"""Append the Stage 21c changelog entry and checkpoint row (idempotent)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = """
## [Stage 21c] — End-to-end pipeline — 2026-11-22
### Added
- `src/incident_agent/pipeline/`: `investigate()` and `python -m incident_agent.pipeline run` (shadow / assist modes),
  diagnosis-to-command classification, client stack (trace, cascade, window), related-knowledge lookup, per-run dashboard.
- `Deps.diagnoser`, `eval.harness.use_client`, `tool_loop.hardened()`, `build_prompt(harden=...)`.
- Attacks INJ-12 (tool output) and INJ-13 (`build_prompt`) — both blocked; 33 attacks total.
- `docs/STAGE21C_PIPELINE.md`, `openspec/changes/030-pipeline/`, `tests/test_pipeline.py`.
### Known limits
- No real-LLM run was made in the build environment; the hardening default outside the pipeline is still off (INJ-09 exposed).
"""
ROW = "| 23 | 2026-11-22 | Stage 21c end-to-end pipeline, shadow mode, INJ-09 closed in the pipeline | stage21c-patch.zip | 35 pipeline tests, security 0 vulnerable |\n"
log = ROOT / "CHANGELOG.md"
if log.exists() and "[Stage 21c]" not in log.read_text(encoding="utf-8"):
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + "\n" + ENTRY, encoding="utf-8")
    print("updated CHANGELOG.md")
cp = ROOT / "docs" / "CHECKPOINTS.md"
if cp.exists() and "Stage 21c" not in cp.read_text(encoding="utf-8"):
    cp.write_text(cp.read_text(encoding="utf-8").rstrip("\n") + "\n" + ROW, encoding="utf-8")
    print("updated docs/CHECKPOINTS.md")
