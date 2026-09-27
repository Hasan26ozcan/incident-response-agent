"""Retrieval layer for Stage 10 — Vector DB & Hybrid Retrieval.

Provides:
  - BM25Retriever: keyword-based BM25+ retrieval over incident text
  - DenseRetriever: numpy TF-IDF cosine-similarity retrieval
  - HybridRetriever: RRF-fused combination of BM25 and dense results
  - RetrievalAgent: agentic wrapper producing validated HybridSearchResult

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""
