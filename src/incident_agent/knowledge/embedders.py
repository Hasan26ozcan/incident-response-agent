"""Embedders for the dense side of hybrid retrieval (Stage 10).

* ``HashingEmbedder``: deterministic, dependency-free signed feature hashing of stemmed tokens and bigrams. It is
  a *lexical* baseline and the test double. It is NOT semantic: do not read its dense scores as "semantic search".
* ``SentenceTransformerEmbedder``: a real local embedding model (free, no API). Needs the optional ``ml`` extra
  (``pip install -e .[ml]``) and a one-time model download, so it is opt-in via ``EMBEDDER=st:<model>``.
* ``CachedEmbedder``: on-disk cache keyed by embedder name, kind (document/query) and text.

Every embedder returns L2-normalised float32 rows, so cosine similarity is a dot product.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from pathlib import Path
from typing import Protocol

import numpy as np

from incident_agent.knowledge.text import tokenize

DEFAULT_ST_MODEL = "BAAI/bge-small-en-v1.5"
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbedderError(RuntimeError):
    """Embedder misconfigured or its optional dependency / model is missing."""


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_queries(self, texts: list[str]) -> np.ndarray: ...


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class HashingEmbedder:
    def __init__(self, dim: int = 512, bigrams: bool = True) -> None:
        if dim < 8:
            raise ValueError("dim must be at least 8")
        self.dim = dim
        self.bigrams = bigrams
        self.name = f"hashing-{dim}{'-bi' if bigrams else ''}"

    def _vector(self, text: str) -> np.ndarray:
        tokens = tokenize(text)
        features = list(tokens)
        if self.bigrams:
            features += [f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        vec = np.zeros(self.dim, dtype=np.float64)
        for feature, count in Counter(features).items():
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            sign = -1.0 if (h >> 63) & 1 else 1.0
            vec[h % self.dim] += sign * (1.0 + math.log(count))
        return vec

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalise(np.vstack([self._vector(t) for t in texts]))

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self.embed_documents(texts)


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_ST_MODEL, query_prefix: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise EmbedderError(
                "sentence-transformers is not installed. Run: pip install -e .[ml]   (or use EMBEDDER=hashing)"
            ) from exc
        try:  # pragma: no cover - needs the model download
            self._model = SentenceTransformer(model_name)
        except Exception as exc:  # pragma: no cover
            raise EmbedderError(f"could not load embedding model {model_name!r}: {exc}") from exc
        self.name = f"st:{model_name}"  # pragma: no cover
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)  # pragma: no cover
        if query_prefix is None:  # pragma: no cover
            query_prefix = BGE_QUERY_PREFIX if "bge" in model_name.lower() else ""
        self._query_prefix = query_prefix  # pragma: no cover

    def _encode(self, texts: list[str]) -> np.ndarray:  # pragma: no cover
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalise(np.asarray(self._model.encode(texts, show_progress_bar=False), dtype=np.float64))

    def embed_documents(self, texts: list[str]) -> np.ndarray:  # pragma: no cover
        return self._encode(texts)

    def embed_queries(self, texts: list[str]) -> np.ndarray:  # pragma: no cover
        return self._encode([self._query_prefix + t for t in texts])


class CachedEmbedder:
    """Disk cache around any embedder; a cache hit never touches the model."""

    def __init__(self, inner: Embedder, cache_dir: str | Path) -> None:
        self.inner = inner
        self.name = inner.name
        self.dim = inner.dim
        self.cache_dir = Path(cache_dir)
        self.hits = 0
        self.misses = 0

    def _path(self, kind: str, text: str) -> Path:
        digest = hashlib.sha256(f"{self.name}\n{kind}\n{text}".encode()).hexdigest()
        return self.cache_dir / f"{digest}.npy"

    def _embed(self, kind: str, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        rows: list[np.ndarray | None] = []
        missing: list[int] = []
        for i, text in enumerate(texts):
            path = self._path(kind, text)
            try:
                row = np.load(path)
            except (OSError, ValueError):
                row = None
            if row is None or row.shape != (self.dim,):
                missing.append(i)
                rows.append(None)
            else:
                rows.append(row)
                self.hits += 1
        if missing:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            todo = [texts[i] for i in missing]
            fresh = self.inner.embed_documents(todo) if kind == "doc" else self.inner.embed_queries(todo)
            for i, vector in zip(missing, fresh, strict=True):
                rows[i] = vector
                path = self._path(kind, texts[i])
                tmp = path.with_name(path.stem + f".{os.getpid()}.tmp.npy")
                np.save(tmp, vector)
                os.replace(tmp, path)
                self.misses += 1
        return np.vstack([r for r in rows if r is not None]).astype(np.float32)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed("doc", texts)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._embed("query", texts)


def build_embedder(spec: str | None = None, cache_dir: str | Path | None = None) -> Embedder:
    """``hashing`` (default), ``hashing:<dim>``, ``st`` or ``st:<model name>``; from ``EMBEDDER`` when spec is None."""
    spec = (spec or os.environ.get("EMBEDDER") or "hashing").strip()
    kind, _, arg = spec.partition(":")
    kind = kind.lower()
    inner: Embedder
    if kind == "hashing":
        try:
            inner = HashingEmbedder(int(arg)) if arg else HashingEmbedder()
        except ValueError as exc:
            raise EmbedderError(f"bad hashing embedder spec {spec!r}: {exc}") from exc
    elif kind in ("st", "sentence-transformers"):
        inner = SentenceTransformerEmbedder(arg or DEFAULT_ST_MODEL)
    else:
        raise EmbedderError(f"unknown embedder {spec!r}; use 'hashing', 'hashing:<dim>' or 'st:<model>'")
    directory = cache_dir or os.environ.get("EMBED_CACHE_DIR")
    return CachedEmbedder(inner, directory) if directory else inner
