"""Reciprocal Rank Fusion and the hybrid searcher (Stage 10).

Standard RRF: ``score(d) = sum over rankers of weight / (k + rank_in_that_ranker)``. A document a ranker did not
return contributes nothing. (The legacy ``retrieval/hybrid.py`` gave absent documents a made-up rank of
``len(results) + 1`` and still added their term, which inflates documents only one ranker found.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from incident_agent.knowledge.bm25 import BM25Index, Hit
from incident_agent.knowledge.corpus import KBDoc
from incident_agent.knowledge.embedders import Embedder
from incident_agent.knowledge.vector_store import NumpyVectorStore, VectorStore

MODES = ("bm25", "dense", "hybrid")
DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class Fused:
    doc_id: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)  # ranker name -> 1-based rank (absent = not returned)


def rrf_fuse(
    rankings: Mapping[str, Sequence[str]],
    k: int = DEFAULT_RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[Fused]:
    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name in sorted(rankings):
        weight = 1.0 if weights is None else weights.get(name, 1.0)
        if weight < 0:
            raise ValueError("RRF weights must be non-negative")
        seen: set[str] = set()
        for position, doc_id in enumerate(rankings[name], start=1):
            if doc_id in seen:  # a ranker listing a document twice counts only its best position
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + position)
            ranks.setdefault(doc_id, {})[name] = position
    ordered = sorted(scores.items(), key=lambda item: (-round(item[1], 12), item[0]))
    return [Fused(doc_id, round(score, 8), ranks[doc_id]) for doc_id, score in ordered]


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    score: float
    rank: int
    title: str
    doc_type: str
    failure_class: str | None
    ranks: dict[str, int]  # which retriever put it where


class HybridSearcher:
    """BM25 + dense (vector store) + RRF over a knowledge-base corpus; ``mode`` selects one side for ablations."""

    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore | None = None,
        *,
        rrf_k: int = DEFAULT_RRF_K,
        depth: int = 20,
        weights: Mapping[str, float] | None = None,
        bm25: BM25Index | None = None,
    ) -> None:
        if depth <= 0:
            raise ValueError("depth must be positive")
        self.embedder = embedder
        self.store: VectorStore = store or NumpyVectorStore()
        self.bm25 = bm25 or BM25Index()
        self.rrf_k = rrf_k
        self.depth = depth
        self.weights = dict(weights) if weights else None
        self._docs: dict[str, KBDoc] = {}

    def __len__(self) -> int:
        return len(self._docs)

    def index(self, docs: Sequence[KBDoc]) -> None:
        ids = [d.id for d in docs]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate document ids in corpus")
        self._docs = {d.id: d for d in docs}
        texts = {d.id: d.search_text for d in docs}
        self.bm25.build(texts)
        ordered = sorted(texts)
        vectors = self.embedder.embed_documents([texts[i] for i in ordered])
        self.store.reset(self.embedder.dim)
        self.store.add(ordered, vectors)

    def _dense(self, query: str, k: int) -> list[Hit]:
        vector = self.embedder.embed_queries([query])[0]
        return self.store.search(vector, k)

    def search(self, query: str, k: int = 5, mode: str = "hybrid") -> list[SearchHit]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if k <= 0 or not self._docs or not query.strip():
            return []
        depth = max(self.depth, k)
        if mode == "bm25":
            hits = self.bm25.search(query, depth)
            fused = [Fused(h.doc_id, h.score, {"bm25": h.rank}) for h in hits]
        elif mode == "dense":
            hits = [h for h in self._dense(query, depth) if h.score > 0]
            fused = [Fused(h.doc_id, h.score, {"dense": h.rank}) for h in hits]
        else:
            bm = [h.doc_id for h in self.bm25.search(query, depth)]
            dn = [h.doc_id for h in self._dense(query, depth) if h.score > 0]
            fused = rrf_fuse({"bm25": bm, "dense": dn}, self.rrf_k, self.weights)
        out: list[SearchHit] = []
        for rank, f in enumerate(fused[:k], start=1):
            doc = self._docs[f.doc_id]
            out.append(SearchHit(doc.id, f.score, rank, doc.title, doc.doc_type, doc.failure_class, f.ranks))
        return out
