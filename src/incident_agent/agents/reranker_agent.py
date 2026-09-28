"""RerankerAgent for Stage 11 — Cross-Encoder Re-ranking.

The RerankerAgent takes hybrid retrieval results (BM25 + Dense + RRF)
from RetrievalAgent and re-ranks the top-N candidates using a
cross-encoder that scores query-document pairs jointly. It produces
a RerankedResult with before/after comparison metrics.

This allows downstream agents (Diagnosis, Debate, Tree-of-Thought)
to receive more accurately ranked evidence, improving the quality
of knowledge-grounded incident diagnosis.

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

import time
from typing import Any

from incident_agent.retrieval.reranker import CrossEncoderReranker
from incident_agent.schemas.reranking import (
    RelevanceDelta,
    RerankedResult,
)
from incident_agent.schemas.vector_search import SearchHit


class RerankerAgent:
    """Agentic interface to the cross-encoder reranker.

    Takes hybrid retrieval results and re-ranks the top-N
    candidates using a cross-encoder, producing a RerankedResult
    with before/after relevance comparison.
    """

    def __init__(self, top_k_candidates: int = 20, top_k_rerank: int = 10) -> None:
        self.reranker = CrossEncoderReranker(top_k_candidates=top_k_candidates)
        self.top_k_rerank = top_k_rerank
        self._documents: dict[str, str] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._initialized = False

    def build_index(
        self,
        documents: dict[str, str],
        metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Build the cross-encoder index from incident text data.

        Args:
            documents: Mapping of incident_id to text content.
            metadata: Optional mapping of incident_id to metadata dicts.
        """
        self._documents = documents
        self.reranker.build_index(documents)
        self._metadata = metadata or {}
        self._initialized = True

    def search(self, query: str, incident_ids: list[str] | None = None) -> RerankedResult:
        """Perform cross-encoder reranking on hybrid retrieval results.

        Pipeline:
        1. Run hybrid retrieval (BM25 + Dense + RRF) via RetrievalAgent
        2. Extract top-N candidate incident IDs from hybrid results
        3. Re-rank candidates using cross-encoder
        4. Produce RerankedResult with before/after comparison

        Args:
            query: Search query string.
            incident_ids: Optional filter to specific incident IDs.

        Returns:
            A validated RerankedResult with before/after metrics.
        """
        start_time = time.monotonic()

        if not self._initialized:
            return RerankedResult(
                agent_type="reranker_agent",
                incident_id="INC-000",
                query=query,
                confidence=0.0,
                total_results=0,
                retrieval_time_ms=0.0,
            )

        # Step 1: Get hybrid retrieval results
        from incident_agent.agents.retrieval_agent import RetrievalAgent

        retrieval_agent = RetrievalAgent()
        retrieval_agent.build_index(self._documents, metadata=self._metadata)
        hybrid_result = retrieval_agent.search(query, incident_ids=incident_ids)

        # Step 2: Extract candidate IDs from hybrid fused rankings
        candidate_ids = [
            hit.incident_id for hit in hybrid_result.fused_rankings
        ]
        if not candidate_ids:
            candidate_ids = list(self._documents.keys())

        # Step 3: Re-rank using cross-encoder
        rerank_start = time.monotonic()
        reranked = self.reranker.rerank(
            query, candidate_ids, self._documents, top_k=self.top_k_rerank
        )
        rerank_time_ms = (time.monotonic() - rerank_start) * 1000

        # Step 4: Build pre-rerank SearchHit list
        pre_rankings: list[SearchHit] = []
        for rank, hit in enumerate(hybrid_result.fused_rankings):
            pre_rankings.append(
                SearchHit(
                    incident_id=hit.incident_id,
                    score=hit.score,
                    ranker_source="rrf",
                    rank=hit.rank,
                    metadata=self._metadata.get(hit.incident_id, {}),
                )
            )

        # Step 5: Build post-rerank SearchHit list
        post_rankings: list[SearchHit] = []
        reranked_scores: dict[str, float] = {}
        for rank, (incident_id, score, _) in enumerate(reranked):
            reranked_scores[incident_id] = score
            post_rankings.append(
                SearchHit(
                    incident_id=incident_id,
                    score=score,
                    ranker_source="cross_encoder",
                    rank=rank + 1,
                    metadata=self._metadata.get(incident_id, {}),
                )
            )

        # Step 6: Compute relevance deltas
        rrf_scores_map: dict[str, float] = hybrid_result.rrf_scores
        relevance_deltas: list[RelevanceDelta] = []
        for inc_id in set(list(rrf_scores_map.keys()) + list(reranked_scores.keys())):
            rrf_s = rrf_scores_map.get(inc_id, 0.0)
            ce_s = reranked_scores.get(inc_id, 0.0)

            # Find rank in pre and post rankings
            pre_rank = next(
                (h.rank for h in pre_rankings if h.incident_id == inc_id),
                len(pre_rankings) + 1,
            )
            post_rank = next(
                (h.rank for h in post_rankings if h.incident_id == inc_id),
                len(post_rankings) + 1,
            )
            rank_change = pre_rank - post_rank  # negative means improved

            relevance_deltas.append(
                RelevanceDelta(
                    incident_id=inc_id,
                    rrf_score=rrf_s,
                    cross_encoder_score=ce_s,
                    delta=round(ce_s - rrf_s, 6),
                    rank_change=rank_change,
                )
            )

        # Sort deltas by incident_id for determinism
        relevance_deltas.sort(key=lambda d: d.incident_id)

        # Step 7: Compute MRR and Precision@K
        target_ids = set(incident_ids) if incident_ids else set(candidate_ids)
        if not target_ids:
            target_ids = set(self._documents.keys())

        mrr_before = self._compute_mrr(pre_rankings, target_ids)
        mrr_after = self._compute_mrr(post_rankings, target_ids)
        p_at_k_before = self._compute_precision_at_k(pre_rankings, target_ids, k=5)
        p_at_k_after = self._compute_precision_at_k(post_rankings, target_ids, k=5)

        elapsed_ms = (time.monotonic() - start_time) * 1000
        total_results = len(post_rankings)

        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id=incident_ids[0] if incident_ids else "INC-000",
            query=query,
            confidence=0.5,
            pre_rerank_rankings=pre_rankings,
            post_rerank_rankings=post_rankings,
            relevance_deltas=relevance_deltas,
            mean_reciprocal_rank_before=round(mrr_before, 6),
            mean_reciprocal_rank_after=round(mrr_after, 6),
            precision_at_k_before=round(p_at_k_before, 6),
            precision_at_k_after=round(p_at_k_after, 6),
            reranking_time_ms=round(rerank_time_ms, 2),
            total_results=total_results,
            retrieval_time_ms=round(elapsed_ms, 2),
        )

        return result

    @staticmethod
    def _compute_mrr(
        rankings: list[SearchHit],
        target_ids: set[str],
    ) -> float:
        """Compute Mean Reciprocal Rank for target documents."""
        if not target_ids or not rankings:
            return 0.0

        reciprocal_ranks: list[float] = []
        for target_id in target_ids:
            for rank_idx, hit in enumerate(rankings):
                if hit.incident_id == target_id:
                    reciprocal_ranks.append(1.0 / (rank_idx + 1))
                    break
            else:
                reciprocal_ranks.append(0.0)

        return sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0

    @staticmethod
    def _compute_precision_at_k(
        rankings: list[SearchHit],
        target_ids: set[str],
        k: int = 5,
    ) -> float:
        """Compute Precision@K for target documents."""
        if not target_ids or not rankings:
            return 0.0

        top_k = rankings[:k]
        if not top_k:
            return 0.0

        relevant = sum(1 for hit in top_k if hit.incident_id in target_ids)
        return relevant / len(top_k)
