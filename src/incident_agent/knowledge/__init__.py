"""Knowledge layer (Stage 10+): knowledge-base corpus, BM25, embeddings, vector stores and hybrid retrieval."""

from incident_agent.knowledge.bm25 import BM25Index, Hit
from incident_agent.knowledge.corpus import CorpusError, KBDoc, load_corpus, parse_doc
from incident_agent.knowledge.embedders import (
    CachedEmbedder,
    Embedder,
    EmbedderError,
    HashingEmbedder,
    build_embedder,
)
from incident_agent.knowledge.hybrid import Fused, HybridSearcher, SearchHit, rrf_fuse
from incident_agent.knowledge.rerank import (
    CachedReranker,
    CrossEncoderReranker,
    OverlapReranker,
    Reranker,
    RerankerError,
    RerankingSearcher,
    build_reranker,
)
from incident_agent.knowledge.vector_store import (
    NumpyVectorStore,
    QdrantVectorStore,
    VectorStoreError,
    build_store,
)

__all__ = [
    "CachedReranker",
    "CrossEncoderReranker",
    "OverlapReranker",
    "Reranker",
    "RerankerError",
    "RerankingSearcher",
    "build_reranker",
    "BM25Index",
    "CachedEmbedder",
    "CorpusError",
    "Embedder",
    "EmbedderError",
    "Fused",
    "HashingEmbedder",
    "Hit",
    "HybridSearcher",
    "KBDoc",
    "NumpyVectorStore",
    "QdrantVectorStore",
    "SearchHit",
    "VectorStoreError",
    "build_embedder",
    "build_store",
    "load_corpus",
    "parse_doc",
    "rrf_fuse",
]
