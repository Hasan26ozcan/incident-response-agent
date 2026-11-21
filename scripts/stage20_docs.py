"""Append the Stage 20 entries to CHANGELOG.md and ROADMAP.md (idempotent; run once from the project root)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = """
## [Stage 20] — Adversarial Testing & Security — 2026-11-21
### Added
- `src/incident_agent/security/`: sealed approvals, kill-switch, injection scanner/hardener, adversarial corpus,
  31-attack suite and a generated security test report (`docs/SECURITY_REPORT.md`).
- `security` section in the Stage 19 eval gate; `tests/test_security.py`; `docs/STAGE20_SECURITY.md`;
  `openspec/changes/027-adversarial-security/proposal.md`.
### Security fixes
- A forged `approval` signal in a run directory no longer approves anything (decisions are sealed by the gate;
  a bare `Deps()` now dry-runs every approval).
- Incident ids with path syntax are refused by `tools.read_*`; Windows reserved device names are refused as run ids.
### Known gaps (reported, gated by count)
- Request-store tampering (APP-08), semantic deception of the rule-based diagnoser (INJ-04), pattern evasions
  (INJ-07), the LLM prompt builder is not hardened (INJ-09; wiring needs approval).
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


append(ROOT / "CHANGELOG.md", CHANGELOG, "[Stage 20]")
road = ROOT / "ROADMAP.md"
if road.exists():
    text = road.read_text(encoding="utf-8")
    key = "*Output:* security test report."
    if key in text and "docs/STAGE20_SECURITY.md" not in text:
        road.write_text(
            text.replace(key, key + "\n*Status:* implemented (see `docs/STAGE20_SECURITY.md`).", 1), encoding="utf-8"
        )
        print("updated ROADMAP.md")
