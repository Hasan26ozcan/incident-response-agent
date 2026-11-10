"""Append the Stage 18 entries to CHANGELOG.md, ROADMAP.md and .env.example (idempotent; run once from the root)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = """
## [Stage 18] — Human-in-the-Loop & Escalation — 2026-11-09
### Added
- `src/incident_agent/hitl/`: event-sourced approval requests, escalation policy (levels, timeouts, quorum per tier),
  token-authenticated approver roster, notifications (outbox, Slack through the Stage 15 sender), a controller that
  connects the gate to Stage 17 durable runs, CLI and demo (`python -m incident_agent.hitl demo`).
- `tests/test_hitl.py`; `openspec/changes/025-human-in-the-loop/proposal.md`; `docs/STAGE18_HITL.md`.
### Rules
- Timeout escalates and finally expires; an expired request is a refusal, never an approval.
### Verified
- Pending-approval and timeout-escalation scenario, spec conformance test, randomised history test, mutation check.
  Live Slack delivery is only tested against fakes; authentication is a local token, not SSO.
"""
ENV = (
    "\n# Stage 18 approval gate: state directory (policy.json, roster.json, requests/, outbox.jsonl)\n"
    "# INCIDENT_HITL_DIR=.cache/hitl\n"
    "# Approvers export their own token before `approve`/`reject` (never pass it as an argument):\n"
    "# INCIDENT_APPROVER_TOKEN=\n"
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


append(ROOT / "CHANGELOG.md", CHANGELOG, "[Stage 18]")
append(ROOT / ".env.example", ENV, "INCIDENT_HITL_DIR")
road = ROOT / "ROADMAP.md"
if road.exists():
    text = road.read_text(encoding="utf-8")
    key = "*Output:* test scenario covering pending approval and timeout escalation."
    if key in text and "docs/STAGE18_HITL.md" not in text:
        note = "\n*Status:* implemented (see `docs/STAGE18_HITL.md`)."
        road.write_text(text.replace(key, key + note, 1), encoding="utf-8")
        print("updated ROADMAP.md")
