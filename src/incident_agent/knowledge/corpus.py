"""Knowledge-base documents (runbooks, past post-mortems, reference notes) and their loader (Stage 10).

Documents are Markdown files under ``data/kb/`` with a small front matter block::

    ---
    id: rb-cpu-exhaustion
    type: runbook            # runbook | postmortem | reference
    title: Runbook: runaway CPU on a service
    class: cpu_exhaustion    # failure class the document is about; omitted for reference notes
    ---
    body text...

No YAML dependency: the front matter is ``key: value`` lines, and unknown keys are errors so a typo cannot
silently drop a field. The KB is knowledge an organisation would plausibly already have; it is written
independently of the gold answers (``eval/validate_kb.py`` checks the two do not overlap verbatim).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[3]
KB_DIR = ROOT / "data" / "kb"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KEYS = {"id", "type", "title", "class"}
DocType = Literal["runbook", "postmortem", "reference"]


class CorpusError(ValueError):
    pass


class KBDoc(BaseModel):
    id: str
    doc_type: DocType
    title: str = Field(min_length=5)
    failure_class: str | None = None
    text: str = Field(min_length=80)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("id must be a lowercase slug (a-z, 0-9, hyphen)")
        return v

    @property
    def search_text(self) -> str:
        """What gets indexed: the title counts as part of the document."""
        return f"{self.title}\n{self.text}"


def parse_doc(raw: str, source: str = "<string>") -> KBDoc:
    lines = raw.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise CorpusError(f"{source}: missing front matter (file must start with '---')")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise CorpusError(f"{source}: front matter is not closed with '---'") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in _KEYS:
            raise CorpusError(f"{source}: bad front matter line {line!r}; allowed keys: {sorted(_KEYS)}")
        if key in meta:
            raise CorpusError(f"{source}: duplicate front matter key {key!r}")
        meta[key] = value.split(" #")[0].strip() if key != "title" else value.strip()
    missing = {"id", "type", "title"} - set(meta)
    if missing:
        raise CorpusError(f"{source}: missing front matter keys {sorted(missing)}")
    try:
        return KBDoc(
            id=meta["id"],
            doc_type=meta["type"],  # type: ignore[arg-type]
            title=meta["title"],
            failure_class=meta.get("class") or None,
            text="\n".join(lines[end + 1 :]).strip(),
        )
    except ValueError as exc:
        raise CorpusError(f"{source}: {exc}") from exc


def load_corpus(directory: Path | None = None) -> list[KBDoc]:
    folder = directory or KB_DIR
    files = sorted(folder.glob("*.md"))
    if not files:
        raise CorpusError(f"no knowledge-base documents (*.md) found in {folder}")
    docs = [parse_doc(f.read_text(encoding="utf-8"), f.name) for f in files]
    for doc, path in zip(docs, files, strict=True):
        if doc.id != path.stem:
            raise CorpusError(f"{path.name}: id {doc.id!r} must equal the file name {path.stem!r}")
    ids = [d.id for d in docs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CorpusError(f"duplicate document ids: {dupes}")
    return docs
