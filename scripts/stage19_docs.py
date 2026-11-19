"""Append the Stage 19 entries to CHANGELOG.md and ROADMAP.md (idempotent; run once from the project root)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = """
## [Stage 19] — Agentic Eval Harness — 2026-11-18
### Added
- `src/incident_agent/agenteval/`: end-to-end scenarios over the real durable engine and approval gate (simulated
  clock and humans), task-completion / efficiency / safety metrics judged on the sandbox backend wire, negative
  controls, baseline gating, CLI (`python -m incident_agent.agenteval run|list`).
- Existing diagnosis, retrieval and memory evals folded in as gated sections (dev split only).
- `eval/agentic/baseline.json`, `.github/workflows/eval.yml` (push, pull request, nightly),
  `tests/test_agenteval.py`, `docs/STAGE19_AGENTIC_EVAL.md`, `openspec/changes/026-agentic-eval/proposal.md`.
### Known gaps
- A forged `approval` signal written to a run directory bypasses the gate (reported, not gated; Stage 20).
### Verified
- Mutation check on the metrics, the gate and the rig; the isolation backend is simulated and the CI workflow
  itself is unverified until its first run on GitHub.
"""


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


append(ROOT / "CHANGELOG.md", CHANGELOG, "[Stage 19]")
road = ROOT / "ROADMAP.md"
if road.exists():
    text = road.read_text(encoding="utf-8")
    key = "*Output:* eval suite running automatically in CI."
    if key in text and "docs/STAGE19_AGENTIC_EVAL.md" not in text:
        note = "\n*Status:* implemented (see `docs/STAGE19_AGENTIC_EVAL.md`)."
        road.write_text(text.replace(key, key + note, 1), encoding="utf-8")
        print("updated ROADMAP.md")
