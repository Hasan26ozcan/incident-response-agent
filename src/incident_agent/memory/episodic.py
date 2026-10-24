"""Episodic memory (Stage 12): remember resolved incidents and recall them when a similar one appears.

An *episode* is what an on-call engineer would write down after an incident: what was observed, what was first
believed, what it turned out to be, and what was learned. Memory is searched with the same hybrid retriever as the
knowledge base (BM25 + dense + RRF), over the *observations* only, so a new incident is matched on what it looks
like, never on a label.

Honesty rules this module enforces:

* The resolved class is the outcome of the post-mortem. It is written *after* the diagnosis was made and is never
  visible while the same incident is being diagnosed (the evaluation protocol is in ``eval/memory_eval.py``).
* "Self-improvement" here means remembered outcomes and lessons that change what is recalled next time. No prompt,
  weight or threshold is rewritten.
* Persistence is an explicit directory (``EPISODE_MEMORY_DIR`` or the ``path`` argument), never the source tree
  (the legacy ``memory/store.py`` wrote JSON files next to the code).
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from incident_agent.knowledge.corpus import KBDoc
from incident_agent.knowledge.embedders import Embedder
from incident_agent.knowledge.hybrid import DEFAULT_RRF_K, HybridSearcher
from incident_agent.knowledge.vector_store import VectorStore

EPISODES_FILE = "episodes.jsonl"


class EpisodeMemoryError(RuntimeError):
    """The episode file is unreadable or an episode is invalid."""


class Reflection(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str
    lesson: str


class Episode(BaseModel):
    model_config = ConfigDict(frozen=True)

    incident_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    created_at: str
    observation: str = Field(min_length=3)
    resolved_class: str = Field(min_length=1)
    resolution: str = ""
    diagnosed_class: str | None = None
    is_false_alarm: bool = False
    reflection: Reflection | None = None

    @property
    def diagnosis_correct(self) -> bool | None:
        return None if self.diagnosed_class is None else self.diagnosed_class == self.resolved_class


def reflect(diagnosed_class: str | None, resolved_class: str, recalled_class: str | None = None) -> Reflection:
    """Deterministic reflection: compare the class that was believed with the class that was confirmed.

    Classes are compared as classes (a controlled vocabulary), not as free text.
    """
    if diagnosed_class is None:
        return Reflection(outcome="unclassified", lesson=f"No class was assigned; resolved as {resolved_class}.")
    if diagnosed_class == resolved_class:
        extra = " Memory had pointed the same way." if recalled_class == resolved_class else ""
        return Reflection(outcome="confirmed", lesson=f"Diagnosis {resolved_class} was confirmed.{extra}")
    note = f" Memory had suggested {recalled_class}." if recalled_class else ""
    return Reflection(
        outcome="misdiagnosed",
        lesson=f"Believed {diagnosed_class} but it was {resolved_class}; check {resolved_class} signals first.{note}",
    )


@dataclass(frozen=True)
class Recollection:
    episode: Episode
    score: float  # fused retrieval score (RRF: rank-based, not calibrated)
    rank: int
    similarity: float = 0.0  # dense cosine between query and episode: the calibrated signal used for gating


@dataclass(frozen=True)
class Prediction:
    label: str | None  # None = memory abstains
    similarity: float  # dense cosine of the best-ranked episode
    share: float  # fraction of the recalled score mass behind ``label``
    recollections: tuple[Recollection, ...] = field(default_factory=tuple)


_PREFIX = "Observed symptoms recorded for this past incident episode and its surrounding monitoring context: "


def _doc(e: Episode) -> KBDoc:
    return KBDoc(
        id=f"ep-{e.incident_id.lower()}",
        doc_type="postmortem",
        title=f"Episode {e.incident_id}",
        failure_class=e.resolved_class,
        text=f"{_PREFIX}{e.observation}",
    )


class EpisodicMemory:
    def __init__(
        self,
        embedder: Embedder,
        path: str | Path | None = None,
        store: VectorStore | None = None,
        *,
        rrf_k: int = DEFAULT_RRF_K,
        depth: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        directory = path or os.environ.get("EPISODE_MEMORY_DIR")
        self.path = Path(directory) if directory else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._searcher = HybridSearcher(embedder, store, rrf_k=rrf_k, depth=depth)
        self._episodes: dict[str, Episode] = {}
        self._dirty = True
        if self.path is not None:
            self._load()

    def __len__(self) -> int:
        return len(self._episodes)

    @property
    def episodes(self) -> list[Episode]:
        return [self._episodes[i] for i in sorted(self._episodes)]

    def now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _file(self) -> Path:
        assert self.path is not None
        return self.path / EPISODES_FILE

    def _load(self) -> None:
        f = self._file()
        if not f.exists():
            return
        try:
            lines = f.read_text().splitlines()
        except OSError as exc:
            raise EpisodeMemoryError(f"cannot read {f}: {exc}") from exc
        for n, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                e = Episode.model_validate_json(line)
            except ValueError as exc:
                raise EpisodeMemoryError(f"cannot read {f} (line {n}): {exc}") from exc
            self._episodes[e.incident_id] = e

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self._file().with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text("".join(e.model_dump_json() + "\n" for e in self.episodes))
        os.replace(tmp, self._file())

    def add(self, episode: Episode) -> None:
        """Store an episode; a later episode for the same incident id replaces the earlier one."""
        self._episodes[episode.incident_id] = episode
        self._dirty = True
        self._persist()

    def record(
        self,
        incident_id: str,
        observation: str,
        resolved_class: str,
        *,
        resolution: str = "",
        diagnosed_class: str | None = None,
        recalled_class: str | None = None,
        is_false_alarm: bool = False,
    ) -> Episode:
        """Write the post-mortem episode, including the deterministic reflection on the earlier diagnosis."""
        episode = Episode(
            incident_id=incident_id,
            created_at=self.now(),
            observation=observation,
            resolved_class=resolved_class,
            resolution=resolution,
            diagnosed_class=diagnosed_class,
            is_false_alarm=is_false_alarm,
            reflection=reflect(diagnosed_class, resolved_class, recalled_class),
        )
        self.add(episode)
        return episode

    def forget(self, incident_id: str) -> bool:
        if incident_id not in self._episodes:
            return False
        del self._episodes[incident_id]
        self._dirty = True
        self._persist()
        return True

    def _ensure_index(self) -> None:
        if self._dirty:
            self._searcher.index([_doc(e) for e in self.episodes])
            self._dirty = False

    def recall(self, query: str, k: int = 3) -> list[Recollection]:
        if not self._episodes:
            return []
        self._ensure_index()
        by_doc = {f"ep-{e.incident_id.lower()}": e for e in self._episodes.values()}
        cosine = {h.doc_id: h.score for h in self._searcher._dense(query, len(self._episodes))}
        return [
            Recollection(by_doc[h.doc_id], h.score, h.rank, round(cosine.get(h.doc_id, 0.0), 6))
            for h in self._searcher.search(query, k, "hybrid")
        ]

    def predict(self, query: str, k: int = 3) -> Prediction:
        """Score-weighted vote of the recalled episodes' resolved classes; abstains when nothing is recalled."""
        recalled = self.recall(query, k)
        if not recalled:
            return Prediction(None, 0.0, 0.0)
        votes: dict[str, float] = defaultdict(float)
        for r in recalled:
            votes[r.episode.resolved_class] += r.score
        total = sum(votes.values())
        label = min(votes, key=lambda c: (-votes[c], c))
        return Prediction(label, recalled[0].similarity, votes[label] / total if total else 0.0, tuple(recalled))

    def lessons_for(self, query: str, k: int = 3) -> Sequence[str]:
        return [r.episode.reflection.lesson for r in self.recall(query, k) if r.episode.reflection is not None]
