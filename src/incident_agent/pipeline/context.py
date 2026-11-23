"""Related knowledge for the report: runbooks and past postmortems that match this incident's errors (read-only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from incident_agent.knowledge.corpus import load_corpus
from incident_agent.knowledge.embedders import build_embedder
from incident_agent.knowledge.hybrid import HybridSearcher
from incident_agent.knowledge.query import QueryError, build_log_query
from incident_agent.knowledge.vector_store import build_store
from incident_agent.tools import _incident_dir


def related_knowledge(incident_id: str, k: int = 3) -> list[dict[str, Any]]:
    """Top-k knowledge-base hits for the incident. Never raises: no match or no data is an empty list."""
    try:
        base: Path = _incident_dir(incident_id)
        query = build_log_query(incident_id, base.parent)
        searcher = HybridSearcher(build_embedder("hashing"), build_store("numpy"))
        searcher.index(load_corpus())
        hits = searcher.search(query, k=k)
    except (QueryError, ValueError, OSError):
        return []
    return [
        {
            "doc_id": h.doc_id,
            "title": h.title,
            "type": h.doc_type,
            "failure_class": h.failure_class,
            "score": round(h.score, 4),
        }
        for h in hits
    ]
