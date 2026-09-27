"""Dense retrieval using numpy TF-IDF embeddings — Stage 10.

Computes TF-IDF vectors for all incident text fields and performs
cosine-similarity retrieval. Uses numpy for efficient matrix
operations without external ML framework dependencies.

Works alongside BM25Retriever to provide complementary retrieval
signals that are fused via Reciprocal Rank Fusion (RRF).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import NamedTuple

import numpy as np


class DenseHit(NamedTuple):
    """Single dense retrieval result."""

    incident_id: str
    score: float
    rank: int


class DenseRetriever:
    """TF-IDF cosine-similarity dense retrieval.

    Builds a TF-IDF matrix over all incident text data and computes
    cosine similarity between a query vector and document vectors.
    """

    def __init__(self) -> None:
        self._doc_ids: list[str] = []
        self._idf: dict[str, float] = {}
        self._vocabulary: dict[str, int] = {}
        self._tfidf_matrix: np.ndarray | None = None
        self._initialized = False

    def _tokenize(self, text: str) -> list[str]:
        """Lowercase and split text into tokens."""
        return re.findall(r"[a-z0-9_]+", text.lower())

    def _build_vocabulary(self, documents: dict[str, str]) -> None:
        """Build term vocabulary and compute IDF values."""
        df: dict[str, int] = defaultdict(int)
        all_tokens: set[str] = set()

        for doc_id in documents:
            tokens = set(self._tokenize(documents[doc_id]))
            all_tokens.update(tokens)
            for token in tokens:
                df[token] += 1

        self._doc_count = len(documents)
        self._vocabulary = {token: idx for idx, token in enumerate(sorted(all_tokens))}

        # Compute IDF: log((N + 1) / (df + 1)) + 1
        self._idf = {token: math.log((self._doc_count + 1) / (df_val + 1)) + 1 for token, df_val in df.items()}

    def _compute_tfidf(self, doc_id: str, text: str) -> np.ndarray:
        """Compute TF-IDF vector for a single document."""
        vector = np.zeros(len(self._vocabulary), dtype=np.float64)
        tokens = self._tokenize(text)
        total = len(tokens)

        if total == 0:
            return vector

        # Term frequency
        tf: dict[str, int] = defaultdict(int)
        for token in tokens:
            tf[token] += 1

        for token, count in tf.items():
            if token in self._vocabulary:
                idx = self._vocabulary[token]
                # TF: count / total tokens
                tf_val = count / total
                # TF-IDF = TF * IDF
                vector[idx] = tf_val * self._idf.get(token, 0.0)

        return vector

    def build_index(self, documents: dict[str, str]) -> None:
        """Build the TF-IDF index from incident text data.

        Args:
            documents: Mapping of incident_id to text content.
        """
        self._doc_ids = sorted(documents.keys())
        self._doc_count = len(self._doc_ids)

        if self._doc_count == 0:
            self._initialized = True
            return

        # Build vocabulary and IDF
        self._build_vocabulary(documents)

        # Compute TF-IDF matrix for all documents
        vectors = []
        for doc_id in self._doc_ids:
            vec = self._compute_tfidf(doc_id, documents[doc_id])
            vectors.append(vec)

        self._tfidf_matrix = np.vstack(vectors) if vectors else np.zeros((0, len(self._vocabulary)), dtype=np.float64)

        # Normalize to unit length for cosine similarity
        norms = np.linalg.norm(self._tfidf_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # avoid division by zero
        self._tfidf_matrix = self._tfidf_matrix / norms

        self._initialized = True

    def search(self, query: str, top_k: int = 10) -> list[DenseHit]:
        """Search using cosine similarity over TF-IDF vectors.

        Args:
            query: Query string to search for.
            top_k: Number of top results to return.

        Returns:
            List of DenseHit objects sorted by descending similarity.
        """
        if not self._initialized or self._tfidf_matrix is None:
            return []

        query_vec = self._compute_tfidf("", query)
        if query_vec.sum() == 0:
            return []

        # Normalize query vector
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []
        query_vec = query_vec / query_norm

        # Cosine similarity: dot product with unit-normalized docs
        similarities = self._tfidf_matrix.dot(query_vec)

        # Get top-k indices
        top_indices = np.argsort(-similarities)[:top_k]

        results: list[DenseHit] = []
        for rank, idx in enumerate(top_indices):
            score = float(similarities[idx])
            if score > 0:
                results.append(
                    DenseHit(
                        incident_id=self._doc_ids[idx],
                        score=round(score, 4),
                        rank=rank + 1,
                    )
                )

        return results
