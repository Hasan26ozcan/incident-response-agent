"""Episodic Memory store — Stage 12.

Writes post-mortems, embeds them via dense retrieval text,
and allows retrieval by similarity + tag filter.
"""

from __future__ import annotations

from pathlib import Path

from incident_agent.schemas.memory import MemoryRecord

MEMORY_DIR = Path(__file__).resolve().parent / "store"


def _ensure_dir() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def save_memory(record: MemoryRecord) -> Path:
    _ensure_dir()
    path = MEMORY_DIR / f"{record.post_mortem.incident_id}.json"
    path.write_text(record.model_dump_json(indent=2))
    return path


def load_memory(incident_id: str) -> MemoryRecord | None:
    _ensure_dir()
    path = MEMORY_DIR / f"{incident_id}.json"
    if not path.exists():
        return None
    return MemoryRecord.model_validate_json(path.read_text())


def list_memories() -> list[MemoryRecord]:
    _ensure_dir()
    records: list[MemoryRecord] = []
    for p in MEMORY_DIR.glob("*.json"):
        records.append(MemoryRecord.model_validate_json(p.read_text()))
    return records


def retrieve_by_tag(tag: str) -> list[MemoryRecord]:
    return [r for r in list_memories() if tag in r.post_mortem.tags]
