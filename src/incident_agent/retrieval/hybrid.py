"""Hybrid retrieval with Reciprocal Rank Fusion — Stage 10.

Combines BM25 keyword retrieval and dense TF-IDF cosine-similarity
retrieval using Reciprocal Rank Fusion (RRF) to produce a unified
ranked result set. RRF is rank-based and does not require score
normalization across different rankers.

RRF formula: score(d) = sum(1 / (k + rank(d)))
where k is typically 60 and rank is 1-based position in each ranker's
result list. Documents not present in a ranker are treated as having
rank = len(results) + 1.

All retrieval components produce validated Pydantic objects.
"""

from __future__ import annotations

from typing import NamedTuple

from incident_agent.retrieval.bm25 import BM25Hit, BM25Retriever
from incident_agent.retrieval.dense import DenseHit, DenseRetriever


class RRFRank(NamedTuple):
    """A single result from RRF fusion."""

    incident_id: str
    score: float
    bm25_rank: int
    dense_rank: int
    rank: int


def rrf_rank(
    bm25_results: list[BM25Hit],
    dense_results: list[DenseHit],
    k: int = 60,
) -> list[RRFRank]:
    """Fuse BM25 and dense rankings using Reciprocal Rank Fusion.

    Args:
        bm25_results: Sorted BM25 results (by descending score).
        dense_results: Sorted dense results (by descending similarity).
        k: RRF constant. Higher k reduces the impact of rank differences.

    Returns:
        List of RRFRank objects sorted by descending RRF score.
    """
    # Build lookup maps: incident_id -> rank (1-based)
    bm25_map: dict[str, int] = {hit.incident_id: hit.rank for hit in bm25_results}
    dense_map: dict[str, int] = {hit.incident_id: hit.rank for hit in dense_results}

    # Collect all unique incident IDs
    all_ids: set[str] = set(bm25_map.keys()) | set(dense_map.keys())
    if not all_ids:
        return []

    # Compute RRF score for each document
    scores: dict[str, float] = {}
    max_bm25_rank = len(bm25_results) if bm25_results else 1
    max_dense_rank = len(dense_results) if dense_results else 1

    for doc_id in all_ids:
        # Rank is 1-based; documents not in results get max_rank + 1
        bm25_rank = bm25_map.get(doc_id, max_bm25_rank + 1)
        dense_rank = dense_map.get(doc_id, max_dense_rank + 1)

        # RRF score: sum of 1/(k + rank) for each ranker
        rrf_score = 1.0 / (k + bm25_rank) + 1.0 / (k + dense_rank)
        scores[doc_id] = round(rrf_score, 6)

    # Sort by descending RRF score
    sorted_ids = sorted(scores.items(), key=lambda x: -x[1])

    results: list[RRFRank] = []
    for rank, (doc_id, score) in enumerate(sorted_ids):
        bm25_r = bm25_map.get(doc_id, -1)
        dense_r = dense_map.get(doc_id, -1)
        results.append(
            RRFRank(
                incident_id=doc_id,
                score=score,
                bm25_rank=bm25_r,
                dense_rank=dense_r,
                rank=rank + 1,
            )
        )

    return results


class HybridRetriever:
    """Orchestrates BM25 + Dense retrieval and fuses via RRF.

    Pipeline:
    1. BM25Retriever searches text keywords
    2. DenseRetriever searches TF-IDF embeddings
    3. rrf_rank() fuses both into a unified ranking
    4. Returns top-k fused results
    """

    def __init__(self, rrf_k: int = 60, top_k: int = 10) -> None:
        self.rrf_k = rrf_k
        self.top_k = top_k
        self.bm25 = BM25Retriever()
        self.dense = DenseRetriever()
        self._initialized = False

    def build_index(self, documents: dict[str, str]) -> None:
        """Build both BM25 and dense indexes from incident text.

        Args:
            documents: Mapping of incident_id to text content.
        """
        self.bm25.build_index(documents)
        self.dense.build_index(documents)
        self._initialized = True

    def search(self, query: str) -> list[RRFRank]:
        """Perform hybrid search combining BM25 and dense results.

        Args:
            query: Query string to search for.

        Returns:
            List of RRFRank objects sorted by descending RRF score,
            limited to top_k results.
        """
        if not self._initialized:
            return []

        bm25_results = self.bm25.search(query, top_k=self.top_k)
        dense_results = self.dense.search(query, top_k=self.top_k)

        fused = rrf_rank(bm25_results, dense_results, k=self.rrf_k)
        return fused[: self.top_k]
