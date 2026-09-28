"""Cross-encoder reranker — Stage 11.

Re-ranks top-N candidates from the hybrid retriever using
a cross-encoder approach that scores query-document pairs
jointly. Unlike Stage 10's RRF (which independently ranks
documents then fuses), the cross-encoder computes a
single relevance score for each (query, document) pair.

The cross-encoder simulates a feed-forward neural network
over concatenated query-document TF-IDF features with
interaction terms. It is deterministic and requires no
external ML framework, staying consistent with the project's
numpy-only dependency pattern.

All reranking components produce validated Pydantic objects
when used through RerankerAgent (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

import numpy as np


class CrossEncoderReranker:
    """Cross-encoder reranker for query-document pairs.

    Takes top-N candidates from the hybrid retriever and
    re-scores each (query, document) pair jointly using a
    deterministic cross-encoder simulation:

    1. Build TF-IDF vocabulary from documents
    2. For each (query, doc) pair, compute concatenated
       feature vector (TF-IDF + BM25 + interaction terms)
    3. Score through a small MLP with fixed weights
    4. Return re-ranked results by descending cross-encoder score

    The cross-encoder's key advantage over bi-encoders (like
    BM25 and dense TF-IDF) is that it models query-document
    interactions directly rather than encoding them separately.
    """

    def __init__(self, top_k_candidates: int = 20, hidden_dim: int = 64) -> None:
        self.top_k_candidates = top_k_candidates
        self.hidden_dim = hidden_dim
        self._vocabulary: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._doc_ids: list[str] = []
        self._tfidf_matrix: np.ndarray | None = None
        self._bm25_scores: dict[str, float] = {}
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
        self._idf = {
            token: math.log((self._doc_count + 1) / (df_val + 1)) + 1
            for token, df_val in df.items()
        }

    def _compute_tfidf(self, text: str) -> np.ndarray:
        """Compute TF-IDF vector for a text."""
        vector = np.zeros(len(self._vocabulary), dtype=np.float64)
        tokens = self._tokenize(text)
        total = len(tokens)

        if total == 0:
            return vector

        tf: dict[str, int] = defaultdict(int)
        for token in tokens:
            tf[token] += 1

        for token, count in tf.items():
            if token in self._vocabulary:
                idx = self._vocabulary[token]
                vector[idx] = (count / total) * self._idf.get(token, 0.0)

        return vector

    def _build_bm25_index(self, documents: dict[str, str]) -> None:
        """Build a lightweight BM25 index for baseline scores."""
        self._bm25_scores = defaultdict(float)
        doc_lengths: dict[str, int] = {}

        for doc_id in documents:
            tokens = self._tokenize(documents[doc_id])
            doc_lengths[doc_id] = len(tokens)

        avg_doc_length = (
            sum(doc_lengths.values()) / len(doc_lengths) if doc_lengths else 1.0
        )
        k1, b = 1.5, 0.75
        N = len(documents)

        for query_token in {
            token
            for text in documents.values()
            for token in self._tokenize(text)
        }:
            if query_token not in self._vocabulary:
                continue
            df = sum(
                1 for text in documents.values() if query_token in self._tokenize(text)
            )
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)

            for doc_id, doc_len in doc_lengths.items():
                tf_count = self._tokenize(documents[doc_id]).count(query_token)
                if tf_count > 0:
                    tf_component = (tf_count * (k1 + 1)) / (
                        tf_count + k1 * (1 - b + b * doc_len / avg_doc_length)
                    )
                    self._bm25_scores[doc_id] += idf * (tf_component + k1)

    def _build_tfidf_matrix(self, documents: dict[str, str]) -> None:
        """Compute and normalize TF-IDF matrix for all documents."""
        self._doc_ids = sorted(documents.keys())
        if not self._doc_ids:
            return

        vectors = []
        for doc_id in self._doc_ids:
            vec = self._compute_tfidf(documents[doc_id])
            vectors.append(vec)

        self._tfidf_matrix = np.vstack(vectors) if vectors else np.zeros(
            (0, len(self._vocabulary)), dtype=np.float64
        )

        # Normalize to unit length
        norms = np.linalg.norm(self._tfidf_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._tfidf_matrix = self._tfidf_matrix / norms

    def _compute_interaction_features(
        self, query_vec: np.ndarray, doc_vec: np.ndarray, bm25_score: float
    ) -> np.ndarray:
        """Compute cross-encoder interaction features.

        Features include:
        - Element-wise product (cross-attention simulation)
        - BM25 score as a feature
        - Query and doc norm features
        """
        # Element-wise product simulates cross-attention
        interaction = query_vec * doc_vec

        # Additional scalar features
        dot_product = float(np.dot(query_vec, doc_vec))
        bm25_normalized = min(bm25_score / 10.0, 1.0) if bm25_score > 0 else 0.0

        # Concatenate: [interaction, dot_product, bm25_normalized]
        features = np.concatenate([
            interaction,
            [dot_product, bm25_normalized],
        ])
        return features

    def _mlp_score(self, features: np.ndarray) -> float:
        """Score a feature vector through a deterministic MLP.

        Uses fixed weights (no training) to produce a deterministic
        relevance score. Architecture: input → hidden (ReLU) → output.
        """
        # Fixed weights with deterministic seed
        rng = np.random.RandomState(42)
        w1 = rng.randn(features.shape[0], self.hidden_dim) * 0.1
        b1 = np.zeros(self.hidden_dim)
        w2 = rng.randn(self.hidden_dim, 1) * 0.1
        b2 = np.zeros(1)

        # Forward pass
        hidden = np.maximum(0.0, features @ w1 + b1)  # ReLU
        score = float((hidden @ w2 + b2).item())

        # Clamp to non-negative range
        return max(score, 0.0)

    def build_index(self, documents: dict[str, str]) -> None:
        """Build the cross-encoder index from document text.

        Args:
            documents: Mapping of incident_id to text content.
        """
        if not documents:
            self._initialized = True
            return

        self._build_vocabulary(documents)
        self._build_tfidf_matrix(documents)
        self._build_bm25_index(documents)
        self._initialized = True

    def _get_top_candidates(
        self,
        query: str,
        candidates: list[str],
        top_k: int,
    ) -> list[str]:
        """Select top candidates from the candidate pool using TF-IDF similarity.

        Args:
            query: Search query string.
            candidates: Pool of candidate document IDs.
            top_k: Maximum number of candidates to return.

        Returns:
            List of candidate document IDs sorted by TF-IDF similarity.
        """
        if not self._initialized or not candidates:
            return []

        query_vec = self._compute_tfidf(query)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return candidates[:top_k]

        query_vec = query_vec / query_norm

        scores: dict[str, float] = {}
        for doc_id in candidates:
            if doc_id in self._doc_ids and self._tfidf_matrix is not None:
                idx = self._doc_ids.index(doc_id)
                doc_vec = self._tfidf_matrix[idx]
                scores[doc_id] = float(np.dot(query_vec, doc_vec))

        sorted_docs = sorted(scores.items(), key=lambda x: -x[1])
        return [doc_id for doc_id, _ in sorted_docs[:top_k]]

    def _score_pair(self, query: str, doc_id: str, documents: dict[str, str]) -> float:
        """Score a single (query, document) pair using the cross-encoder.

        Args:
            query: Query string.
            doc_id: Document identifier.
            documents: Full document corpus.

        Returns:
            Cross-encoder relevance score.
        """
        if not self._initialized or doc_id not in documents:
            return 0.0

        query_vec = self._compute_tfidf(query)
        doc_vec = self._compute_tfidf(documents[doc_id])
        query_norm = np.linalg.norm(query_vec)
        doc_norm = np.linalg.norm(doc_vec)

        if query_norm == 0 or doc_norm == 0:
            bm25_score = self._bm25_scores.get(doc_id, 0.0)
            return bm25_score

        query_vec = query_vec / query_norm
        doc_vec = doc_vec / doc_norm

        bm25_score = self._bm25_scores.get(doc_id, 0.0)
        features = self._compute_interaction_features(query_vec, doc_vec, bm25_score)

        return self._mlp_score(features)

    def rerank(
        self,
        query: str,
        candidate_ids: list[str],
        documents: dict[str, str],
        top_k: int = 10,
    ) -> list[tuple[str, float, int]]:
        """Re-rank candidate documents using the cross-encoder.

        Args:
            query: Original search query.
            candidate_ids: List of candidate document IDs to re-rank.
            documents: Full document corpus for TF-IDF computation.
            top_k: Number of top re-ranked results to return.

        Returns:
            List of (incident_id, cross_encoder_score, rank) tuples,
            sorted by descending cross-encoder score.
        """
        if not self._initialized or not candidate_ids:
            return []

        # Score each candidate pair
        scores: dict[str, float] = {}
        for doc_id in candidate_ids:
            score = self._score_pair(query, doc_id, documents)
            if score > 0:
                scores[doc_id] = round(score, 6)

        if not scores:
            # Fallback: return candidates with zero scores
            results = [
                (doc_id, 0.0, rank + 1)
                for rank, doc_id in enumerate(candidate_ids[:top_k])
            ]
            return results

        # Sort by descending score, then by doc_id for determinism
        sorted_scores = sorted(scores.items(), key=lambda x: (-x[1], x[0]))

        results = [
            (doc_id, score, rank + 1)
            for rank, (doc_id, score) in enumerate(sorted_scores[:top_k])
        ]
        return results

    def search(
        self,
        query: str,
        candidate_ids: list[str],
        documents: dict[str, str],
        top_k: int = 10,
    ) -> list[tuple[str, float, int]]:
        """Convenience method: get top candidates and re-rank them.

        Args:
            query: Search query string.
            candidate_ids: Pool of candidate document IDs.
            documents: Full document corpus.
            top_k: Number of top results to return.

        Returns:
            List of (incident_id, cross_encoder_score, rank) tuples.
        """
        # First filter to top_k_candidates from the candidate pool
        filtered = self._get_top_candidates(query, candidate_ids, self.top_k_candidates)
        return self.rerank(query, filtered, documents, top_k=top_k)
