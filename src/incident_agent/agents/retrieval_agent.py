"""RetrievalAgent for Stage 10 — Vector DB & Hybrid Retrieval.

The RetrievalAgent provides an agentic interface to the hybrid
search layer. It accepts a query over incident data and returns
a HybridSearchResult containing BM25, dense, and RRF-fused results.

This allows downstream agents (Diagnosis, Debate, Tree-of-Thought)
to retrieve relevant historical incidents as evidence during
reasoning, enabling knowledge-grounded incident diagnosis.

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

import time
from typing import Any

from incident_agent.retrieval.hybrid import HybridRetriever
from incident_agent.schemas.vector_search import HybridSearchResult, SearchHit


class RetrievalAgent:
    """Agentic interface to the hybrid retrieval layer.

    Provides search capability over all incident data, combining
    BM25 keyword matching, dense TF-IDF similarity, and RRF
    rank fusion into a single unified result set.
    """

    def __init__(self, rrf_k: int = 60, top_k: int = 10) -> None:
        self.retriever = HybridRetriever(rrf_k=rrf_k, top_k=top_k)
        self._documents: dict[str, str] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._initialized = False

    def build_index(
        self,
        documents: dict[str, str],
        metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Build the hybrid search index from incident text data.

        Args:
            documents: Mapping of incident_id to text content.
            metadata: Optional mapping of incident_id to metadata
                dicts (for enriching SearchHit results).
        """
        self._documents = documents
        self.retriever.build_index(documents)
        self._metadata = metadata or {}
        self._initialized = True

    def search(self, query: str, incident_ids: list[str] | None = None) -> HybridSearchResult:
        """Perform hybrid search over incident data.

        Pipeline:
        1. Run BM25 keyword search
        2. Run dense TF-IDF search
        3. RRF fusion of both rankers
        4. Produce HybridSearchResult

        Args:
            query: Search query string.
            incident_ids: Optional filter to specific incident IDs.

        Returns:
            A validated HybridSearchResult with fused rankings.
        """
        start_time = time.monotonic()

        if not self._initialized:
            return HybridSearchResult(
                agent_type="retrieval_agent",
                incident_id="INC-000",
                query=query,
                total_results=0,
                retrieval_time_ms=0.0,
            )

        # Run both rankers
        bm25_results = self.retriever.bm25.search(query, top_k=self.retriever.top_k)
        dense_results = self.retriever.dense.search(query, top_k=self.retriever.top_k)

        # Fuse via RRF
        fused = self.retriever.search(query)

        # Build SearchHit lists from rankers
        bm25_hits: list[SearchHit] = [
            SearchHit(
                incident_id=hit.incident_id,
                score=hit.score,
                ranker_source="bm25",
                rank=hit.rank,
                metadata=self._metadata.get(hit.incident_id, {}),
            )
            for hit in bm25_results
        ]

        dense_hits: list[SearchHit] = [
            SearchHit(
                incident_id=hit.incident_id,
                score=hit.score,
                ranker_source="dense",
                rank=hit.rank,
                metadata=self._metadata.get(hit.incident_id, {}),
            )
            for hit in dense_results
        ]

        # Build fused SearchHit list from RRFRank results
        fused_hits: list[SearchHit] = []
        rrf_scores: dict[str, float] = {}
        for rank_obj in fused:
            rrf_scores[rank_obj.incident_id] = rank_obj.score
            fused_hits.append(
                SearchHit(
                    incident_id=rank_obj.incident_id,
                    score=rank_obj.score,
                    ranker_source="rrf",
                    rank=rank_obj.rank,
                    metadata=self._metadata.get(rank_obj.incident_id, {}),
                )
            )

        elapsed_ms = (time.monotonic() - start_time) * 1000

        result = HybridSearchResult(
            agent_type="retrieval_agent",
            incident_id=incident_ids[0] if incident_ids else "INC-000",
            query=query,
            bm25_results=bm25_hits,
            dense_results=dense_hits,
            rrf_scores=rrf_scores,
            fused_rankings=fused_hits,
            total_results=len(fused_hits),
            retrieval_time_ms=round(elapsed_ms, 2),
        )

        return result
