"""Okapi BM25 over a small document collection (Stage 10).

Replaces the legacy ``retrieval/bm25.py``, whose inverted index stored each document once per term, so the
term frequency it computed was always 1 (a document repeating "cpu" five times scored *lower* than one that
mentioned it once). Here the postings keep real term frequencies, the IDF is the non-negative Lucene form, and
the ranking is deterministic (ties broken by document id).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from incident_agent.knowledge.text import tokenize


@dataclass(frozen=True)
class Hit:
    doc_id: str
    score: float
    rank: int  # 1-based


class BM25Index:
    def __init__(self, k1: float = 1.2, b: float = 0.75, tokenizer: Callable[[str], list[str]] = tokenize) -> None:
        if k1 < 0 or not 0.0 <= b <= 1.0:
            raise ValueError("BM25 needs k1 >= 0 and 0 <= b <= 1")
        self.k1 = k1
        self.b = b
        self._tokenize = tokenizer
        self._postings: dict[str, dict[str, int]] = {}
        self._lengths: dict[str, int] = {}
        self._avg_len = 0.0

    def __len__(self) -> int:
        return len(self._lengths)

    def build(self, docs: Mapping[str, str]) -> None:
        self._postings = {}
        self._lengths = {}
        for doc_id in sorted(docs):
            counts = Counter(self._tokenize(docs[doc_id]))
            self._lengths[doc_id] = sum(counts.values())
            for term, tf in counts.items():
                self._postings.setdefault(term, {})[doc_id] = tf
        self._avg_len = sum(self._lengths.values()) / len(self._lengths) if self._lengths else 0.0

    def idf(self, term: str) -> float:
        n = len(self._lengths)
        df = len(self._postings.get(term, ()))
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 10) -> list[Hit]:
        if k <= 0 or not self._lengths:
            return []
        scores: dict[str, float] = {}
        for term in sorted(set(self._tokenize(query))):
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for doc_id, tf in postings.items():
                norm = 1.0 - self.b + self.b * (self._lengths[doc_id] / self._avg_len if self._avg_len else 0.0)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (self.k1 + 1.0) / (tf + self.k1 * norm)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
        return [Hit(doc_id, round(score, 6), i + 1) for i, (doc_id, score) in enumerate(ranked)]
