"""Append the Stage 21b changelog entry (idempotent)."""

from pathlib import Path

p = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
entry = """
## [Stage 21b] — Fault Lab — 2026-11-22
### Added
- `src/incident_agent/lab/`: two real services with injectable faults, load generator, capture into the incident-folder
  format, `lab run|run-all|eval`, Docker Compose lab (`lab/`), `lab.live` driver; 5 captured incidents in `data/lab/`.
- `docs/STAGE21B_FAULT_LAB.md`, `docs/CHECKPOINTS.md`, `openspec/changes/029-fault-lab/`, `tests/test_lab.py`.
### Changed
- Incident ids `LAB-nnn` accepted in schemas, data tools and the sandbox allow-list; `harness.evaluate(pattern=...)`.
### Finding
- The rule-based baseline answers 0/5 live captures (reality gap vs the synthetic dataset).
"""
if p.exists() and "[Stage 21b]" not in p.read_text(encoding="utf-8"):
    p.write_text(p.read_text(encoding="utf-8").rstrip("\n") + "\n" + entry, encoding="utf-8")
    print("updated CHANGELOG.md")
else:
    print("skip CHANGELOG.md")
