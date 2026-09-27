"""Vector search schemas — Stage 10.

Provides Pydantic models for hybrid search results combining
BM25 keyword retrieval, dense TF-IDF retrieval, and RRF fusion.

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from incident_agent.schemas.agent_output import AgentOutput


class SearchHit(BaseModel):
    """A single search result from any ranker."""

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident identifier")
    score: float = Field(ge=0.0, description="Relevance score")
    ranker_source: str = Field(min_length=1, description="Source of the ranking ('bm25', 'dense', or 'rrf')")
    rank: int = Field(ge=1, description="Rank position in the result list")
    metadata: dict = Field(default_factory=dict, description="Additional result metadata")

    @field_validator("score")
    @classmethod
    def score_must_be_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("score must be non-negative")
        return v


class HybridSearchResult(AgentOutput):
    """Combined hybrid search result from BM25 + Dense + RRF.

    Stage 10: The retrieval layer fuses multiple ranking signals
    into a single unified result set. Each result carries both the
    original ranker scores and the fused RRF score.

    All fields are validated by Pydantic.
    """

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident being searched for")
    query: str = Field(min_length=1, description="Search query")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Retrieval confidence score")
    bm25_results: list[SearchHit] = Field(
        default_factory=list,
        description="Raw BM25 keyword retrieval results",
    )
    dense_results: list[SearchHit] = Field(
        default_factory=list,
        description="Raw dense TF-IDF retrieval results",
    )
    rrf_scores: dict[str, float] = Field(
        default_factory=dict,
        description="Mapping of incident_id to RRF fused score",
    )
    fused_rankings: list[SearchHit] = Field(
        default_factory=list,
        description="Fused results ranked by RRF score",
    )
    total_results: int = Field(default=0, ge=0, description="Total number of fused results")
    retrieval_time_ms: float = Field(default=0.0, ge=0.0, description="Total retrieval time in milliseconds")

    def format_report(self) -> str:
        """Return a human-readable hybrid search report."""
        lines = [
            f"=== Hybrid Search Report: {self.incident_id} ===",
            f"Query: {self.query}",
            f"Total Results: {self.total_results}",
            f"Retrieval Time: {self.retrieval_time_ms:.2f}ms",
            "",
            "BM25 Results:",
        ]
        for hit in self.bm25_results[:5]:
            lines.append(f"  #{hit.rank}: {hit.incident_id} (score={hit.score:.4f})")
        lines.append("")
        lines.append("Dense Results:")
        for hit in self.dense_results[:5]:
            lines.append(f"  #{hit.rank}: {hit.incident_id} (score={hit.score:.4f})")
        lines.append("")
        lines.append("Fused Rankings (RRF):")
        for hit in self.fused_rankings[:10]:
            bm25_r = next(
                (h.rank for h in self.bm25_results if h.incident_id == hit.incident_id),
                "-",
            )
            dense_r = next(
                (h.rank for h in self.dense_results if h.incident_id == hit.incident_id),
                "-",
            )
            lines.append(f"  #{hit.rank}: {hit.incident_id} (rrf={hit.score:.6f}, bm25={bm25_r}, dense={dense_r})")
        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Return JSON serialization of the hybrid search result."""
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str) -> HybridSearchResult:
        """Parse a HybridSearchResult from a JSON string."""
        return cls.model_validate_json(data)

    def model_dump(self, *args, **kwargs) -> dict:
        """Return a dict representation."""
        return super().model_dump(*args, **kwargs)
