"""Vector stores for dense retrieval (Stage 10): an exact numpy store and Qdrant (local or server).

The roadmap names Qdrant. ``qdrant-client`` runs fully in-process (``:memory:`` or a local path), so the free,
no-server setup keeps working; point ``QDRANT_URL`` at a server later without code changes. Both stores return
the same ranking for the same data (ties broken by id) so they are interchangeable and tested for parity.
"""

from __future__ import annotations

import hashlib
import os
from typing import Protocol

import numpy as np

from incident_agent.knowledge.bm25 import Hit


class VectorStoreError(RuntimeError):
    pass


class VectorStore(Protocol):
    def reset(self, dim: int) -> None: ...

    def add(self, ids: list[str], vectors: np.ndarray) -> None: ...

    def search(self, vector: np.ndarray, k: int) -> list[Hit]: ...

    def count(self) -> int: ...


def _check(ids: list[str], vectors: np.ndarray, dim: int | None) -> None:
    if len(ids) != len(vectors):
        raise VectorStoreError(f"{len(ids)} ids but {len(vectors)} vectors")
    if len(set(ids)) != len(ids):
        raise VectorStoreError("duplicate ids in one add() call")
    if dim is not None and vectors.size and vectors.shape[1] != dim:
        raise VectorStoreError(f"vector dimension {vectors.shape[1]} does not match store dimension {dim}")


class NumpyVectorStore:
    """Exact cosine search (vectors are expected to be L2-normalised, so cosine == dot product)."""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._dim: int | None = None

    def reset(self, dim: int) -> None:
        self._ids = []
        self._matrix = np.zeros((0, dim), dtype=np.float32)
        self._dim = dim

    def add(self, ids: list[str], vectors: np.ndarray) -> None:
        _check(ids, vectors, self._dim)
        clash = set(ids) & set(self._ids)
        if clash:
            raise VectorStoreError(f"ids already stored: {sorted(clash)[:3]}")
        if not ids:
            return
        if self._dim is None:
            self.reset(vectors.shape[1])
        self._ids.extend(ids)
        self._matrix = np.vstack([self._matrix, vectors.astype(np.float32)])

    def count(self) -> int:
        return len(self._ids)

    def search(self, vector: np.ndarray, k: int) -> list[Hit]:
        if k <= 0 or not self._ids:
            return []
        sims = self._matrix @ vector.astype(np.float32)
        order = sorted(range(len(self._ids)), key=lambda i: (-round(float(sims[i]), 6), self._ids[i]))[:k]
        return [Hit(self._ids[i], round(float(sims[i]), 6), r + 1) for r, i in enumerate(order)]


class QdrantVectorStore:
    """Qdrant through ``qdrant-client``. ``location`` is ``:memory:``, a local path, or an http(s) URL."""

    def __init__(self, location: str | None = None, collection: str = "incident_kb") -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise VectorStoreError("qdrant-client is not installed. Run: pip install -e .[ml]") from exc
        location = location or os.environ.get("QDRANT_URL") or ":memory:"
        self.collection = collection
        if location.startswith(("http://", "https://")):
            self._client = QdrantClient(url=location)  # pragma: no cover - needs a server
        elif location == ":memory:":
            self._client = QdrantClient(location=":memory:")
        else:
            self._client = QdrantClient(path=location)
        self._ids: dict[int, str] = {}
        self._dim: int | None = None

    @staticmethod
    def _point_id(doc_id: str) -> int:
        return int.from_bytes(hashlib.blake2b(doc_id.encode(), digest_size=8).digest(), "big") >> 1

    def reset(self, dim: int) -> None:
        from qdrant_client import models

        if self._client.collection_exists(self.collection):
            self._client.delete_collection(self.collection)
        self._client.create_collection(
            self.collection, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
        )
        self._ids = {}
        self._dim = dim

    def add(self, ids: list[str], vectors: np.ndarray) -> None:
        from qdrant_client import models

        _check(ids, vectors, self._dim)
        if not ids:
            return
        if self._dim is None:
            self.reset(vectors.shape[1])
        points = []
        for doc_id, vector in zip(ids, vectors, strict=True):
            pid = self._point_id(doc_id)
            if pid in self._ids:
                raise VectorStoreError(f"id already stored (or hash collision): {doc_id!r}")
            self._ids[pid] = doc_id
            points.append(models.PointStruct(id=pid, vector=[float(x) for x in vector], payload={"doc_id": doc_id}))
        self._client.upsert(self.collection, points=points)

    def count(self) -> int:
        return len(self._ids)

    def search(self, vector: np.ndarray, k: int) -> list[Hit]:
        if k <= 0 or not self._ids:
            return []
        points = self._client.query_points(
            self.collection, query=[float(x) for x in vector], limit=max(k, len(self._ids))
        ).points
        scored = [(str(p.payload["doc_id"]), round(float(p.score), 6)) for p in points if p.payload]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [Hit(doc_id, score, r + 1) for r, (doc_id, score) in enumerate(scored[:k])]


def build_store(kind: str | None = None) -> VectorStore:
    """``numpy`` (default) or ``qdrant``; from ``VECTOR_STORE`` when kind is None."""
    kind = (kind or os.environ.get("VECTOR_STORE") or "numpy").strip().lower()
    if kind == "numpy":
        return NumpyVectorStore()
    if kind == "qdrant":
        return QdrantVectorStore()
    raise VectorStoreError(f"unknown vector store {kind!r}; use 'numpy' or 'qdrant'")
