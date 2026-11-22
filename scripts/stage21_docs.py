"""Append the Stage 21 entries to CHANGELOG.md and ROADMAP.md (idempotent; run once from the project root)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = """
## [Stage 21] — Observability & Cost — 2026-11-21
### Added
- `src/incident_agent/observe/`: local span tracing (JSONL), secret redaction, `TracedClient`, verified price table,
  `CascadeClient` (cheap → strong), `SlidingWindow`, prompt-cache analyser, traces derived from durable journals,
  optional LangSmith / Phoenix (OTLP) / W&B adapters, cost-latency report and self-contained dashboard.
- `observability` section (6 checks, 3 negative controls) in the Stage 19 eval gate; `tests/test_observe.py`;
  `docs/STAGE21_OBSERVABILITY.md`; `openspec/changes/028-observability-cost/proposal.md`.
### Changed
- `OpenAICompatClient` reports `usage["cached_tokens"]` when the provider returns a positive value.
- CI hygiene: `# nosec` markers on reviewed bandit findings in `sandbox/backends.py`, `# ruff: noqa: E501` in
  `scripts/stage16_docs.py`.
### Known limits
- Cascade saving in the gate is a mechanics test with scripted models; exporters are tested against fakes.
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


append(ROOT / "CHANGELOG.md", CHANGELOG, "[Stage 21]")
road = ROOT / "ROADMAP.md"
if road.exists():
    text = road.read_text(encoding="utf-8")
    key = "dashboard + cost/latency report."
    if key in text and "docs/STAGE21_OBSERVABILITY.md" not in text:
        road.write_text(
            text.replace(key, key + " ✅ Implemented — see `docs/STAGE21_OBSERVABILITY.md`.", 1), encoding="utf-8"
        )
        print("updated ROADMAP.md")
    else:
        print("skip ROADMAP.md: marker missing or already done")
