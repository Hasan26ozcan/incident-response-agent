"""Reranking (Stage 11): re-score the hybrid top-N with a model that reads query and document together.

* ``OverlapReranker``: deterministic, dependency-free baseline and test double. It scores IDF-weighted query-term
  coverage plus query-bigram coverage (word order). It is *lexical*: do not read its scores as a neural reranker.
* ``CrossEncoderReranker``: a real local cross-encoder (free, no API). Needs the optional ``ml`` extra and a
  one-time model download, so it is opt-in via ``RERANKER=ce:<model>``.
* ``CachedReranker``: on-disk score cache keyed by reranker name, query and document text.
* ``RerankingSearcher``: wraps ``HybridSearcher``; mode ``rerank`` retrieves ``candidates`` documents with hybrid
  search, re-scores them and (optionally) blends the reranker score with the fused retrieval score.

The legacy ``retrieval/reranker.py`` called itself a cross-encoder but was an untrained MLP with random fixed
weights (seed 42) over TF-IDF features, and its "BM25" feature did not depend on the query at all
(see ``docs/AUDIT-STAGES-10-12.md``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from incident_agent.knowledge.corpus import KBDoc
from incident_agent.knowledge.hybrid import MODES, HybridSearcher, SearchHit
from incident_agent.knowledge.text import tokenize
from incident_agent.knowledge.vector_store import VectorStore

RERANK_MODE = "rerank"
ALL_MODES = (*MODES, RERANK_MODE)
DEFAULT_CE_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
BIGRAM_WEIGHT = 0.5


class RerankerError(RuntimeError):
    """Reranker misconfigured or its optional dependency / model is missing."""


class Reranker(Protocol):
    name: str

    def fit(self, docs: Mapping[str, str]) -> None: ...

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class OverlapReranker:
    name = "overlap"

    def __init__(self, bigram_weight: float = BIGRAM_WEIGHT) -> None:
        if bigram_weight < 0:
            raise ValueError("bigram_weight must be non-negative")
        self.bigram_weight = bigram_weight
        self._idf: dict[str, float] = {}
        self._default_idf = 1.0

    def fit(self, docs: Mapping[str, str]) -> None:
        n = len(docs)
        df: Counter[str] = Counter()
        for text in docs.values():
            df.update(set(tokenize(text)))
        self._idf = {t: math.log(1.0 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._default_idf = math.log(1.0 + (n + 0.5) / 0.5) if n else 1.0  # a term no document has: maximally rare

    def _one(self, query_tokens: list[str], text: str) -> float:
        if not query_tokens:
            return 0.0
        doc_tokens = tokenize(text)
        present = set(doc_tokens)
        terms = set(query_tokens)
        total = sum(self._idf.get(t, self._default_idf) for t in terms)
        coverage = sum(self._idf.get(t, self._default_idf) for t in terms if t in present) / total if total else 0.0
        q_bi = set(zip(query_tokens, query_tokens[1:], strict=False))
        d_bi = set(zip(doc_tokens, doc_tokens[1:], strict=False))
        bigram = len(q_bi & d_bi) / len(q_bi) if q_bi else 0.0
        return coverage + self.bigram_weight * bigram

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        q = tokenize(query)
        return [self._one(q, t) for t in texts]


class CrossEncoderReranker:
    def __init__(self, model_name: str = DEFAULT_CE_MODEL) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise RerankerError(
                "sentence-transformers is not installed. Run: pip install -e .[ml]   (or use RERANKER=overlap)"
            ) from exc
        try:  # pragma: no cover - needs the model download
            self._model = CrossEncoder(model_name)
        except Exception as exc:  # pragma: no cover
            raise RerankerError(f"could not load cross-encoder {model_name!r}: {exc}") from exc
        self.name = f"ce:{model_name}"  # pragma: no cover

    def fit(self, docs: Mapping[str, str]) -> None:  # pragma: no cover - nothing to learn
        return None

    def score(self, query: str, texts: Sequence[str]) -> list[float]:  # pragma: no cover
        if not texts:
            return []
        return [float(s) for s in self._model.predict([(query, t) for t in texts], show_progress_bar=False)]


class CachedReranker:
    """Disk cache around any reranker; a cache hit never touches the model."""

    def __init__(self, inner: Reranker, cache_dir: str | Path) -> None:
        self.inner = inner
        self.name = inner.name
        self.cache_dir = Path(cache_dir)
        self.hits = 0
        self.misses = 0

    def fit(self, docs: Mapping[str, str]) -> None:
        self.inner.fit(docs)

    def _path(self, query: str, text: str) -> Path:
        digest = hashlib.sha256(f"{self.name}\n{query}\n{text}".encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        out: list[float | None] = []
        missing: list[int] = []
        for i, text in enumerate(texts):
            try:
                value = float(json.loads(self._path(query, text).read_text())["score"])
            except (OSError, ValueError, KeyError, TypeError):
                out.append(None)
                missing.append(i)
            else:
                out.append(value)
                self.hits += 1
        if missing:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fresh = self.inner.score(query, [texts[i] for i in missing])
            for i, value in zip(missing, fresh, strict=True):
                out[i] = value
                path = self._path(query, texts[i])
                tmp = path.with_name(path.stem + f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"score": value}))
                os.replace(tmp, path)
                self.misses += 1
        return [v for v in out if v is not None]


def build_reranker(spec: str | None = None, cache_dir: str | Path | None = None) -> Reranker:
    """``overlap``, ``ce`` or ``ce:<model>``; from ``RERANKER`` when spec is None (default ``overlap``)."""
    spec = (spec or os.environ.get("RERANKER") or "overlap").strip()
    kind, _, arg = spec.partition(":")
    inner: Reranker
    if kind.lower() == "overlap":
        inner = OverlapReranker()
    elif kind.lower() in ("ce", "cross-encoder"):
        inner = CrossEncoderReranker(arg or DEFAULT_CE_MODEL)
    else:
        raise RerankerError(f"unknown reranker {spec!r}; use 'overlap' or 'ce:<model>'")
    directory = cache_dir or os.environ.get("RERANK_CACHE_DIR")
    return CachedReranker(inner, directory) if directory else inner


def _minmax(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


class RerankingSearcher:
    """``HybridSearcher`` plus a ``rerank`` mode; the other modes are passed through unchanged."""

    def __init__(
        self,
        base: HybridSearcher,
        reranker: Reranker,
        *,
        candidates: int = 20,
        alpha: float = 0.0,
    ) -> None:
        if candidates <= 0:
            raise ValueError("candidates must be positive")
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be between 0 and 1")
        self.base = base
        self.reranker = reranker
        self.candidates = candidates
        self.alpha = alpha
        self._texts: dict[str, str] = {}

    @property
    def store(self) -> VectorStore:
        return self.base.store

    def __len__(self) -> int:
        return len(self.base)

    def index(self, docs: Sequence[KBDoc]) -> None:
        self.base.index(docs)
        self.reranker.fit({d.id: d.search_text for d in docs})
        self._texts = {d.id: d.search_text for d in docs}

    def search(self, query: str, k: int = 5, mode: str = "hybrid") -> list[SearchHit]:
        if mode != RERANK_MODE:
            return self.base.search(query, k, mode)
        if not query.strip():
            return []
        pool = self.base.search(query, max(self.candidates, k), "hybrid")
        if not pool:
            return []
        scores = self.reranker.score(query, [self._texts[h.doc_id] for h in pool])
        if len(scores) != len(pool):
            raise RerankerError("reranker returned a different number of scores than candidates")
        model = _minmax(scores)
        fused = _minmax([h.score for h in pool])
        final = [(1.0 - self.alpha) * m + self.alpha * f for m, f in zip(model, fused, strict=True)]
        order = sorted(range(len(pool)), key=lambda i: (-round(final[i], 12), pool[i].rank, pool[i].doc_id))
        out: list[SearchHit] = []
        for rank, i in enumerate(order[:k], start=1):
            h = pool[i]
            out.append(
                SearchHit(
                    h.doc_id,
                    round(final[i], 8),
                    rank,
                    h.title,
                    h.doc_type,
                    h.failure_class,
                    {**h.ranks, "hybrid": h.rank, RERANK_MODE: rank},
                )
            )
        return out
