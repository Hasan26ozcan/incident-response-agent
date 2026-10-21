"""Validate the knowledge base and retrieval gold (Stage 10): structure, coverage and leak checks.

Checks, each reported as an ERROR:
* the corpus loads and every document is well formed
* every failure class used by the retrieval gold has a runbook and a post-mortem
* every gold incident exists in data/incidents, in eval/gold and in the dev/test split
* leak: no knowledge-base document and no retrieval query shares a run of ``SHINGLE`` consecutive words with the
  gold answers (root cause, evidence, remediation), and no document names an evaluation incident id

Usage: python -m incident_agent.eval.validate_kb
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from incident_agent.eval.harness import GOLD_DIR, SPLITS_FILE
from incident_agent.eval.retrieval_eval import GOLD_FILE, GoldError, load_gold
from incident_agent.knowledge.corpus import KB_DIR, CorpusError, KBDoc, load_corpus
from incident_agent.knowledge.query import INCIDENTS_DIR

SHINGLE = 6
_WORDS = re.compile(r"[a-z0-9']+")
_INC_ID = re.compile(r"\bINC-\d+\b", re.IGNORECASE)


def shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    words = _WORDS.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _gold_texts(gold_dir: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(gold_dir.glob("INC-*.json")):
        g = json.loads(path.read_text())
        texts = [
            str(g.get("root_cause", "")),
            str(g.get("recommended_remediation", "")),
            str(g.get("risk_rationale", "")),
        ]
        texts += [str(e) for e in g.get("evidence", [])]
        out[path.stem] = [t for t in texts if t]
    return out


def validate(
    kb_dir: Path | None = None,
    gold_file: Path | None = None,
    gold_dir: Path | None = None,
    incidents_dir: Path | None = None,
    splits_file: Path | None = None,
) -> list[str]:
    problems: list[str] = []
    try:
        docs: list[KBDoc] = load_corpus(kb_dir or KB_DIR)
    except CorpusError as exc:
        return [f"corpus: {exc}"]
    try:
        gold: Mapping[str, Mapping[str, Any]] = load_gold(gold_file or GOLD_FILE)
    except (GoldError, OSError, json.JSONDecodeError, KeyError) as exc:
        return [*problems, f"retrieval gold: {exc}"]

    classes_in_gold: set[str] = set()
    for entry in gold.values():
        classes_in_gold.add(entry["primary"])
        classes_in_gold.update(entry.get("secondary", []))
    for cls in sorted(classes_in_gold):
        kinds = {d.doc_type for d in docs if d.failure_class == cls}
        for needed in ("runbook", "postmortem"):
            if needed not in kinds:
                problems.append(f"coverage: failure class {cls!r} has no {needed}")
    known = {d.failure_class for d in docs if d.failure_class}
    for iid, entry in gold.items():
        for cls in [entry["primary"], *entry.get("secondary", [])]:
            if cls not in known:
                problems.append(f"{iid}: class {cls!r} has no document")
    for d in docs:
        if d.doc_type == "reference" and d.failure_class:
            problems.append(f"{d.id}: reference notes must not carry a failure class")
        if d.doc_type != "reference" and not d.failure_class:
            problems.append(f"{d.id}: {d.doc_type} needs a 'class'")

    gold_texts = _gold_texts(gold_dir or GOLD_DIR)
    splits = json.loads((splits_file or SPLITS_FILE).read_text())
    in_split = set(splits.get("dev", [])) | set(splits.get("test", []))
    for iid in gold:
        if iid not in gold_texts:
            problems.append(f"{iid}: no eval/gold answer file")
        if not ((incidents_dir or INCIDENTS_DIR) / iid / "meta.json").is_file():
            problems.append(f"{iid}: no incident data")
        if iid not in in_split:
            problems.append(f"{iid}: not in eval/splits.json")

    gold_shingles = {
        iid: set().union(*(shingles(t) for t in texts)) if texts else set() for iid, texts in gold_texts.items()
    }
    candidates: Iterable[tuple[str, str]] = [(d.id, d.search_text) for d in docs]
    candidates = [
        *candidates,
        *((f"{iid}:{style}", str(e[style])) for iid, e in gold.items() for style in ("paraphrase", "symptom")),
    ]
    for label, text in candidates:
        if _INC_ID.search(text):
            problems.append(f"leak: {label} names an incident id")
        mine = shingles(text)
        for iid, theirs in gold_shingles.items():
            overlap = mine & theirs
            if overlap:
                sample = " ".join(sorted(overlap)[0])
                problems.append(
                    f"leak: {label} shares {SHINGLE}+ consecutive words with the gold answer of {iid}: {sample!r}"
                )
    return problems


def main() -> int:
    problems = validate()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  ERROR {p}")
        return 1
    print("knowledge base and retrieval gold: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
