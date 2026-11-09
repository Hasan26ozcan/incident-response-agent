"""Append the Stage 17 entries to CHANGELOG.md, ROADMAP.md and .env.example (idempotent; run once from the root)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = """
## [Stage 17] — Durable Workflow — 2026-11-08
### Added
- `src/incident_agent/durable/`: event-sourced journal, resumable engine (at-least-once steps, at-most-once `execute`),
  durable waits/signals/cancel, OS file lock, incident workflow on the Stage 16 sandbox, CLI with a SIGKILL resume demo,
  optional Temporal adapter (`pip install "temporalio>=1.7"`).
- `tests/test_durable.py`, `tests/test_temporal_adapter.py`; `openspec/changes/024-durable-workflow/proposal.md`,
  `docs/STAGE17_DURABLE.md`.
### Verified
- Real SIGKILL then resume; mutation check on the durable package. Temporal server tests need a local server.
"""
ROADMAP_NOTE = "\n*Status:* implemented (see `docs/STAGE17_DURABLE.md`).\n"
ENV = "\n# Stage 17 durable workflow: where run journals are stored\n# INCIDENT_RUNS_DIR=.cache/runs\n"


def append(path: Path, text: str, marker: str) -> None:
    if not path.exists():
        print(f"skip {path.name}: not found")
        return
    body = path.read_text(encoding="utf-8")
    if marker in body:
        print(f"skip {path.name}: already done")
        return
    path.write_text(body.rstrip("\n") + "\n" + text, encoding="utf-8")
    print(f"updated {path.name}")


append(ROOT / "CHANGELOG.md", CHANGELOG, "[Stage 17]")
append(ROOT / ".env.example", ENV, "INCIDENT_RUNS_DIR")
road = ROOT / "ROADMAP.md"
if road.exists():
    text = road.read_text(encoding="utf-8")
    key = "*Output:* post-interruption resume demo."
    if key in text and "docs/STAGE17_DURABLE.md" not in text:
        road.write_text(text.replace(key, key + ROADMAP_NOTE, 1), encoding="utf-8")
        print("updated ROADMAP.md")
