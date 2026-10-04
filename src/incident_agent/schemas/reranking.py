"""Reranking schemas — Stage 11.

Provides the RerankedResult Pydantic model for cross-encoder
reranking of hybrid retrieval results. Measures before/after
relevance improvement via MRR and Precision@K metrics.

All outputs are validated Pydantic objects (Stage 4 cross-cutting rule).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field, field_validator

from incident_agent.schemas.agent_output import AgentOutput
from incident_agent.schemas.vector_search import SearchHit


class RelevanceDelta(BaseModel):
    """Change in relevance score for a single document after reranking.

    Tracks how the cross-encoder reranker adjusted the relevance
    score of each document relative to the original RRF score.
    """

    incident_id: str = Field(pattern=r"^INC-\d{3}$", description="Incident identifier")
    rrf_score: float = Field(ge=0.0, description="Original RRF fused score")
    cross_encoder_score: float = Field(ge=0.0, description="Cross-encoder reranked score")
    delta: float = Field(description="Difference: cross_encoder_score - rrf_score")
    rank_change: int = Field(description="Change in rank position (negative = improved)")

    @field_validator("delta")
    @classmethod
    def delta_is_difference(cls, v: float, info) -> float:
        """Delta should reflect cross_encoder_score - rrf_score."""
        return v


class RerankedResult(AgentOutput):
    """Cross-encoder reranked search result with before/after comparison.

    Stage 11: Takes the top-N results from the hybrid retriever
    (BM25 + Dense + RRF) and re-ranks them using a cross-encoder
    that scores query-document pairs jointly. The result includes
    a full before/after comparison with relevance improvement
    metrics.

    All fields are validated by Pydantic.
    """

    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Confidence score for the reranked result")
    query: str = Field(min_length=1, description="Original search query")
    pre_rerank_rankings: list[SearchHit] = Field(
        default_factory=list,
        description="Rankings from RRF fusion before cross-encoder reranking",
    )
    post_rerank_rankings: list[SearchHit] = Field(
        default_factory=list,
        description="Re-ranked results from the cross-encoder",
    )
    relevance_deltas: list[RelevanceDelta] = Field(
        default_factory=list,
        description="Per-document relevance change after reranking",
    )
    mean_reciprocal_rank_before: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="MRR of pre-rerank rankings",
    )
    mean_reciprocal_rank_after: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="MRR of post-rerank rankings",
    )
    precision_at_k_before: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Precision@K of pre-rerank rankings",
    )
    precision_at_k_after: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Precision@K of post-rerank rankings",
    )
    reranking_time_ms: float = Field(default=0.0, ge=0.0, description="Time spent reranking in ms")
    total_results: int = Field(default=0, ge=0, description="Total number of results")
    retrieval_time_ms: float = Field(default=0.0, ge=0.0, description="Total retrieval time in ms")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def improvement_score(self) -> float:
        """Net improvement: (MRR_after + P@K_after) - (MRR_before + P@K_before)."""
        return (self.mean_reciprocal_rank_after + self.precision_at_k_after) - (
            self.mean_reciprocal_rank_before + self.precision_at_k_before
        )

    def format_report(self) -> str:
        """Return a human-readable reranking comparison report."""
        lines = [
            f"=== Reranking Report: {self.query} ===",
            f"Improvement Score: {self.improvement_score:.6f}",
            "",
            "Before Re-Ranking (RRF):",
            f"  MRR: {self.mean_reciprocal_rank_before:.4f}",
            f"  Precision@K: {self.precision_at_k_before:.4f}",
            "",
            "After Re-Ranking (Cross-Encoder):",
            f"  MRR: {self.mean_reciprocal_rank_after:.4f}",
            f"  Precision@K: {self.precision_at_k_after:.4f}",
            "",
            "Relevance Deltas:",
        ]
        for delta in self.relevance_deltas[:10]:
            direction = "↑" if delta.delta > 0 else ("↓" if delta.delta < 0 else "=")
            lines.append(
                f"  {delta.incident_id}: "
                f"rrf={delta.rrf_score:.6f} → ce={delta.cross_encoder_score:.6f} "
                f"({direction}{delta.delta:.6f}, rank_change={delta.rank_change})"
            )
        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Return JSON serialization of the reranked result."""
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str) -> RerankedResult:
        """Parse a RerankedResult from a JSON string."""
        return cls.model_validate_json(data)

    def model_dump(self, *args, **kwargs) -> dict:
        """Return a dict representation."""
        return super().model_dump(*args, **kwargs)
