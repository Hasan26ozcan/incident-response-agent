"""BM25 keyword-based retriever — Stage 10.

Implements BM25+ scoring over tokenized incident text fields
(logs, metrics, metadata). Builds an inverted index from all
incidents and computes BM25+ relevance scores for query terms.

BM25+ differs from standard BM25 by adding k1 to the
denominator to avoid zero scores for unmatched terms.

All retrieval components follow the deterministic, template-based
pattern established in earlier stages (no LLM calls).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import NamedTuple


class BM25Hit(NamedTuple):
    """Single BM25 retrieval result."""

    incident_id: str
    score: float
    rank: int


class BM25Retriever:
    """BM25+ retrieval over incident text collections.

    Builds an inverted index from incident text data and scores
    queries using the BM25+ formula with parameters k1=1.5 and b=0.75.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._index: dict[str, list[str]] = defaultdict(list)
        self._doc_lengths: dict[str, int] = {}
        self._avg_doc_length = 0.0
        self._doc_count = 0
        self._doc_ids: list[str] = []
        self._initialized = False

    def _tokenize(self, text: str) -> list[str]:
        """Lowercase and split text into tokens."""
        return re.findall(r"[a-z0-9_]+", text.lower())

    def build_index(self, documents: dict[str, str]) -> None:
        """Build the inverted index from a mapping of incident_id to text.

        Args:
            documents: Mapping of incident_id to text content.
        """
        self._doc_ids = sorted(documents.keys())
        self._doc_count = len(self._doc_ids)

        # Reset index
        self._index = defaultdict(list)
        self._doc_lengths = {}

        for doc_id in self._doc_ids:
            tokens = self._tokenize(documents[doc_id])
            self._doc_lengths[doc_id] = len(tokens)
            # Store unique tokens per doc for index building
            seen: set[str] = set()
            for token in tokens:
                if token not in seen:
                    self._index[token].append(doc_id)
                    seen.add(token)

        # Compute average document length
        self._avg_doc_length = sum(self._doc_lengths.values()) / self._doc_count if self._doc_count > 0 else 0.0
        self._initialized = True

    def _bm25_plus_score(self, query_tokens: list[str], doc_id: str) -> float:
        """Compute BM25+ score for a query against a single document."""
        if not self._initialized or doc_id not in self._doc_lengths:
            return 0.0

        score = 0.0
        doc_len = self._doc_lengths[doc_id]
        N = self._doc_count

        for token in query_tokens:
            if token not in self._index:
                continue

            df = len(self._index[token])
            if df == 0:
                continue

            # IDF component (BM25+ variant)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)

            # Term frequency component with k1 and b
            tf = self._index[token].count(doc_id)
            tf_component = (
                (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_length))
                if self._avg_doc_length > 0
                else 0.0
            )

            # BM25+ adds k1 to the numerator to avoid zero scores
            score += idf * (tf_component + self.k1)

        return score

    def search(self, query: str, top_k: int = 10) -> list[BM25Hit]:
        """Search the index with a query string.

        Args:
            query: Query string to search for.
            top_k: Number of top results to return.

        Returns:
            List of BM25Hit objects sorted by descending score.
        """
        if not self._initialized:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores: dict[str, float] = {}
        for doc_id in self._doc_ids:
            score = self._bm25_plus_score(query_tokens, doc_id)
            if score > 0:
                scores[doc_id] = score

        # Sort by descending score, then by doc_id for determinism
        sorted_docs = sorted(scores.items(), key=lambda x: (-x[1], x[0]))

        results: list[BM25Hit] = []
        for i, (doc_id, score) in enumerate(sorted_docs[:top_k]):
            results.append(
                BM25Hit(
                    incident_id=doc_id,
                    score=round(score, 4),
                    rank=i + 1,
                )
            )

        return results
