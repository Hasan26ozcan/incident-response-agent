"""Append the Stage 16 entries to CHANGELOG.md, ROADMAP.md and .env.example (idempotent; run once from the root)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = """
## [Stage 16] — Sandbox Code/Shell Execution Safety — 2026-11-07
### Added
- `src/incident_agent/sandbox/`: typed command allow-list (no shell), policy + fingerprint, single-use bound
  approvals (interface for Stage 18), fsynced audit log, `process` / `namespace` / `docker` backends with declared
  guarantees, runner that fails closed, sandbox-escape self-test, CLI `python -m incident_agent.sandbox`.
- `tests/test_sandbox.py`: 124 tests, 99 % line coverage of the package.
- `openspec/changes/023-sandbox-execution/proposal.md`, `docs/STAGE16_SANDBOX.md`.
### Verified
- Namespace backend: 16/16 escape checks pass in the build sandbox; process backend shows the same attacks escaping
  (positive control). Docker backend verified through its command line and a fake CLI only.
"""
ROADMAP_NOTE = "\n*Status:* implemented (see `docs/STAGE16_SANDBOX.md`).\n"
ENV = (
    "\n# Stage 16 sandbox: docker | namespace | process (default: strongest available)\n"
    "# SANDBOX_BACKEND=\n"
    "# SANDBOX_DOCKER_IMAGE=python:3.13-slim\n"
)


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


append(ROOT / "CHANGELOG.md", CHANGELOG, "[Stage 16]")
append(ROOT / ".env.example", ENV, "SANDBOX_BACKEND")
road = ROOT / "ROADMAP.md"
if road.exists():
    text = road.read_text(encoding="utf-8")
    key = "*Output:* isolation verified via sandbox-escape attempt tests."
    if key in text and "docs/STAGE16_SANDBOX.md" not in text:
        road.write_text(text.replace(key, key + ROADMAP_NOTE, 1), encoding="utf-8")
        print("updated ROADMAP.md")
