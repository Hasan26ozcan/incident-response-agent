"""Retrieval layer for Stage 10–11 — Vector DB & Hybrid Retrieval + Re-ranking.

Provides:
  - BM25Retriever: keyword-based BM25+ retrieval over incident text
  - DenseRetriever: numpy TF-IDF cosine-similarity retrieval
  - HybridRetriever: RRF-fused combination of BM25 and dense results
  - RetrievalAgent: agentic wrapper producing validated HybridSearchResult
  - CrossEncoderReranker: cross-encoder reranker for query-document pairs (Stage 11)
  - RerankerAgent: agentic wrapper producing validated RerankedResult (Stage 11)

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

from incident_agent.retrieval.bm25 import BM25Hit, BM25Retriever
from incident_agent.retrieval.dense import DenseHit, DenseRetriever
from incident_agent.retrieval.hybrid import HybridRetriever, RRFRank, rrf_rank
from incident_agent.retrieval.reranker import CrossEncoderReranker

__all__ = [
    "BM25Hit",
    "BM25Retriever",
    "CrossEncoderReranker",
    "DenseHit",
    "DenseRetriever",
    "HybridRetriever",
    "RRFRank",
    "rrf_rank",
]
