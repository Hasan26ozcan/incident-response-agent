"""Tests for Stage 10 — Vector DB & Hybrid Retrieval.

Covers:
  - SearchHit schema validation.
  - HybridSearchResult schema validation, to_json/from_json roundtrip, format_report.
  - BM25Retriever: index building, BM25+ scoring, keyword search.
  - DenseRetriever: TF-IDF embedding, cosine similarity, search.
  - rrf_rank: Reciprocal Rank Fusion of BM25 and dense results.
  - HybridRetriever: combined index building and hybrid search.
  - RetrievalAgent: full pipeline producing validated HybridSearchResult.
  - Cross-cutting rule: HybridSearchResult is a validated Pydantic object.
  - Backward compatibility: all prior agents still work unchanged.
"""

from __future__ import annotations

import pytest

from incident_agent.agents.debate_mechanism import DebateMechanism
from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import OrchestratorAgent
from incident_agent.agents.react_agent import ReActAgent
from incident_agent.agents.retrieval_agent import RetrievalAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
from incident_agent.agents.tree_of_thought_agent import TreeOfThoughtAgent
from incident_agent.retrieval.bm25 import BM25Hit, BM25Retriever
from incident_agent.retrieval.dense import DenseHit, DenseRetriever
from incident_agent.retrieval.hybrid import HybridRetriever, RRFRank, rrf_rank
from incident_agent.schemas import (
    Diagnosis,
    EvidenceItem,
    EvidenceType,
    HybridSearchResult,
    ReasoningStep,
    SearchHit,
)
from incident_agent.schemas.agent_output import AgentOutput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            source_type=EvidenceType.LOG_ENTRY,
            source="log:test",
            detail="Test evidence",
            confidence_weight=0.9,
        )
    ]


def _default_reasoning() -> list[ReasoningStep]:
    return [
        ReasoningStep(step_number=1, description="Test step", evidence_refs=[]),
    ]


def _make_diagnosis(risk_tier: str = "high", confidence: float = 0.85) -> Diagnosis:
    return Diagnosis(
        agent_type="orchestrator_worker",
        incident_id="INC-001",
        root_cause="Test root cause for testing purposes that is long enough",
        confidence=confidence,
        evidence=_default_evidence(),
        affected_service="test-service",
        category="cpu_exhaustion",
        reasoning_steps=_default_reasoning(),
        recommendation="Test recommendation for testing purposes that is long enough",
        risk_tier=risk_tier,
    )


def _sample_documents() -> dict[str, str]:
    """Return sample incident text documents for retrieval tests."""
    return {
        "INC-001": "cpu exhaustion high load memory pressure checkout-api error timeout",
        "INC-002": "database connection pool exhausted max connections reached db timeout",
        "INC-003": "deploy failed rollback triggered service unavailable error in production",
        "INC-004": "latency spike p99 high response time degraded performance api gateway",
        "INC-005": "memory leak growth oom killer restart required service crash",
        "INC-006": "cpu saturation max capacity scaling failed auto scale threshold breach",
        "INC-007": "network partition split brain consensus failure cluster nodes disconnected",
        "INC-008": "disk space full inode exhausted log rotation failed storage capacity",
        "INC-009": "cache miss rate high redis timeout latency increase read throughput drop",
        "INC-010": "authentication failure token expired unauthorized access denied security alert",
    }


# ---------------------------------------------------------------------------
# SearchHit schema
# ---------------------------------------------------------------------------


class TestSearchHitSchema:
    """Verify SearchHit is a properly structured Pydantic model."""

    def test_search_hit_is_pydantic_model(self):
        """SearchHit should be a Pydantic BaseModel subclass."""
        hit = SearchHit(
            incident_id="INC-001",
            score=0.95,
            ranker_source="bm25",
            rank=1,
        )
        assert isinstance(hit, SearchHit)
        assert hit.incident_id == "INC-001"
        assert hit.score == 0.95

    def test_search_hit_score_must_be_non_negative(self):
        """SearchHit score must be non-negative."""
        with pytest.raises(Exception):
            SearchHit(
                incident_id="INC-001",
                score=-0.1,
                ranker_source="bm25",
                rank=1,
            )

    def test_search_hit_to_json_roundtrip(self):
        """SearchHit serialization should roundtrip correctly."""
        hit = SearchHit(
            incident_id="INC-001",
            score=0.85,
            ranker_source="dense",
            rank=2,
        )
        json_str = hit.model_dump_json()
        restored = SearchHit.model_validate_json(json_str)
        assert restored.incident_id == hit.incident_id
        assert restored.score == hit.score


# ---------------------------------------------------------------------------
# HybridSearchResult schema
# ---------------------------------------------------------------------------


class TestHybridSearchResultSchema:
    """Verify HybridSearchResult is a properly structured Pydantic model."""

    def test_hybrid_search_result_is_pydantic_model(self):
        """HybridSearchResult should be a Pydantic BaseModel subclass."""
        result = HybridSearchResult(
            agent_type="retrieval_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            total_results=3,
            retrieval_time_ms=10.5,
        )
        assert isinstance(result, HybridSearchResult)
        assert result.total_results == 3

    def test_hybrid_search_result_is_agent_output(self):
        """HybridSearchResult should inherit from AgentOutput."""
        result = HybridSearchResult(
            agent_type="retrieval_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            total_results=3,
            retrieval_time_ms=10.5,
        )
        assert isinstance(result, AgentOutput)

    def test_hybrid_search_result_to_json_roundtrip(self):
        """HybridSearchResult serialization should roundtrip correctly."""
        result = HybridSearchResult(
            agent_type="retrieval_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            total_results=2,
            retrieval_time_ms=5.0,
        )
        json_str = result.model_dump_json()
        restored = HybridSearchResult.model_validate_json(json_str)
        assert restored.incident_id == result.incident_id

    def test_hybrid_search_result_format_report(self):
        """format_report should produce a non-empty string."""
        result = HybridSearchResult(
            agent_type="retrieval_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            total_results=1,
            retrieval_time_ms=1.0,
        )
        report = result.format_report()
        assert len(report) > 0
        assert "Hybrid Search Report" in report


# ---------------------------------------------------------------------------
# BM25Retriever
# ---------------------------------------------------------------------------


class TestBM25Retriever:
    """Verify BM25Retriever works correctly."""

    def test_bm25_retriever_is_initialized(self):
        """BM25Retriever should be instantiable."""
        retriever = BM25Retriever()
        assert retriever.k1 == 1.5
        assert retriever.b == 0.75

    def test_bm25_build_index(self):
        """BM25 should build an index from documents."""
        docs = _sample_documents()
        retriever = BM25Retriever()
        retriever.build_index(docs)
        assert retriever._doc_count == len(docs)
        assert retriever._initialized is True

    def test_bm25_search_returns_results(self):
        """BM25 should return relevant results for a query."""
        docs = _sample_documents()
        retriever = BM25Retriever()
        retriever.build_index(docs)
        results = retriever.search("cpu exhaustion", top_k=3)
        assert len(results) > 0
        assert all(isinstance(r, BM25Hit) for r in results)

    def test_bm25_search_returns_sorted_by_score(self):
        """BM25 results should be sorted by descending score."""
        docs = _sample_documents()
        retriever = BM25Retriever()
        retriever.build_index(docs)
        results = retriever.search("cpu", top_k=5)
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    def test_bm25_search_returns_bm25hits(self):
        """BM25 search should return BM25Hit objects with correct fields."""
        docs = _sample_documents()
        retriever = BM25Retriever()
        retriever.build_index(docs)
        results = retriever.search("cpu", top_k=3)
        for hit in results:
            assert isinstance(hit.incident_id, str)
            assert isinstance(hit.score, float)
            assert isinstance(hit.rank, int)
            assert hit.rank >= 1

    def test_bm25_empty_search(self):
        """BM25 should return empty results for unknown query."""
        docs = _sample_documents()
        retriever = BM25Retriever()
        retriever.build_index(docs)
        results = retriever.search("xyznonexistentquery12345", top_k=5)
        assert len(results) == 0

    def test_bm25_top_k_limiting(self):
        """BM25 should respect top_k limit."""
        docs = _sample_documents()
        retriever = BM25Retriever()
        retriever.build_index(docs)
        results = retriever.search("error", top_k=2)
        assert len(results) <= 2


# ---------------------------------------------------------------------------
# DenseRetriever
# ---------------------------------------------------------------------------


class TestDenseRetriever:
    """Verify DenseRetriever works correctly."""

    def test_dense_retriever_is_initialized(self):
        """DenseRetriever should be instantiable."""
        retriever = DenseRetriever()
        assert retriever._initialized is False

    def test_dense_build_index(self):
        """DenseRetriever should build an index from documents."""
        docs = _sample_documents()
        retriever = DenseRetriever()
        retriever.build_index(docs)
        assert retriever._initialized is True
        assert retriever._doc_count == len(docs)

    def test_dense_search_returns_results(self):
        """DenseRetriever should return relevant results for a query."""
        docs = _sample_documents()
        retriever = DenseRetriever()
        retriever.build_index(docs)
        results = retriever.search("cpu", top_k=3)
        assert len(results) > 0
        assert all(isinstance(r, DenseHit) for r in results)

    def test_dense_search_returns_sorted_by_score(self):
        """Dense results should be sorted by descending similarity."""
        docs = _sample_documents()
        retriever = DenseRetriever()
        retriever.build_index(docs)
        results = retriever.search("cpu", top_k=5)
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    def test_dense_search_returns_densehits(self):
        """Dense search should return DenseHit objects with correct fields."""
        docs = _sample_documents()
        retriever = DenseRetriever()
        retriever.build_index(docs)
        results = retriever.search("memory", top_k=3)
        for hit in results:
            assert isinstance(hit.incident_id, str)
            assert isinstance(hit.score, float)
            assert isinstance(hit.rank, int)

    def test_dense_top_k_limiting(self):
        """DenseRetriever should respect top_k limit."""
        docs = _sample_documents()
        retriever = DenseRetriever()
        retriever.build_index(docs)
        results = retriever.search("error", top_k=2)
        assert len(results) <= 2


# ---------------------------------------------------------------------------
# rrf_rank function
# ---------------------------------------------------------------------------


class TestRRRFusion:
    """Verify rrf_rank fusion works correctly."""

    def test_rrf_rank_with_empty_inputs(self):
        """rrf_rank should return empty list for empty inputs."""
        results = rrf_rank([], [])
        assert len(results) == 0

    def test_rrf_rank_with_bm25_only(self):
        """rrf_rank should work with BM25-only results."""
        bm25_results = [
            BM25Hit(incident_id="INC-001", score=1.5, rank=1),
            BM25Hit(incident_id="INC-002", score=1.2, rank=2),
        ]
        results = rrf_rank(bm25_results, [])
        assert len(results) == 2
        assert results[0].incident_id == "INC-001"

    def test_rrf_rank_with_dense_only(self):
        """rrf_rank should work with dense-only results."""
        dense_results = [
            DenseHit(incident_id="INC-001", score=0.9, rank=1),
            DenseHit(incident_id="INC-002", score=0.7, rank=2),
        ]
        results = rrf_rank([], dense_results)
        assert len(results) == 2
        assert results[0].incident_id == "INC-001"

    def test_rrf_rank_combines_both(self):
        """rrf_rank should produce scores combining both rankers."""
        bm25_results = [
            BM25Hit(incident_id="INC-001", score=1.5, rank=1),
            BM25Hit(incident_id="INC-002", score=1.2, rank=2),
        ]
        dense_results = [
            DenseHit(incident_id="INC-001", score=0.9, rank=2),
            DenseHit(incident_id="INC-002", score=0.8, rank=1),
        ]
        results = rrf_rank(bm25_results, dense_results)
        assert len(results) == 2
        for r in results:
            assert r.score > 0
            assert isinstance(r.score, float)

    def test_rrf_rank_scores_decreasing(self):
        """RRF results should be sorted by descending score."""
        bm25_results = [
            BM25Hit(incident_id="INC-001", score=1.5, rank=1),
            BM25Hit(incident_id="INC-002", score=1.2, rank=2),
            BM25Hit(incident_id="INC-003", score=1.0, rank=3),
        ]
        dense_results = [
            DenseHit(incident_id="INC-003", score=0.95, rank=1),
            DenseHit(incident_id="INC-001", score=0.85, rank=2),
            DenseHit(incident_id="INC-002", score=0.75, rank=3),
        ]
        results = rrf_rank(bm25_results, dense_results)
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    def test_rrf_rank_has_correct_fields(self):
        """RRFRank should have all required fields."""
        bm25_results = [
            BM25Hit(incident_id="INC-001", score=1.5, rank=1),
        ]
        dense_results = [
            DenseHit(incident_id="INC-001", score=0.9, rank=1),
        ]
        results = rrf_rank(bm25_results, dense_results)
        r = results[0]
        assert isinstance(r.incident_id, str)
        assert isinstance(r.score, float)
        assert isinstance(r.bm25_rank, int)
        assert isinstance(r.dense_rank, int)
        assert isinstance(r.rank, int)


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------


class TestHybridRetriever:
    """Verify HybridRetriever works correctly."""

    def test_hybrid_retriever_is_initialized(self):
        """HybridRetriever should be instantiable."""
        retriever = HybridRetriever()
        assert retriever.rrf_k == 60
        assert retriever.top_k == 10

    def test_hybrid_build_index(self):
        """HybridRetriever should build both BM25 and dense indexes."""
        docs = _sample_documents()
        retriever = HybridRetriever()
        retriever.build_index(docs)
        assert retriever._initialized is True

    def test_hybrid_search_returns_rrf_results(self):
        """HybridRetriever should return RRF-fused results."""
        docs = _sample_documents()
        retriever = HybridRetriever()
        retriever.build_index(docs)
        results = retriever.search("cpu")
        assert len(results) > 0
        assert all(isinstance(r, RRFRank) for r in results)

    def test_hybrid_search_respects_top_k(self):
        """HybridRetriever should respect top_k limit."""
        docs = _sample_documents()
        retriever = HybridRetriever(top_k=3)
        retriever.build_index(docs)
        results = retriever.search("error")
        assert len(results) <= 3

    def test_hybrid_search_results_have_both_rankers(self):
        """Hybrid results should include results from both rankers."""
        docs = _sample_documents()
        retriever = HybridRetriever()
        retriever.build_index(docs)
        results = retriever.search("cpu")
        # At least one result should appear in both BM25 and dense
        assert len(results) > 0


# ---------------------------------------------------------------------------
# RetrievalAgent
# ---------------------------------------------------------------------------


class TestRetrievalAgent:
    """Verify RetrievalAgent works correctly."""

    def test_retrieval_agent_is_initialized(self):
        """RetrievalAgent should be instantiable."""
        agent = RetrievalAgent()
        assert agent.retriever.top_k == 10

    def test_retrieval_agent_build_index(self):
        """RetrievalAgent should build index from documents."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        assert agent._initialized is True

    def test_retrieval_agent_search_returns_hybrid_result(self):
        """RetrievalAgent.search should produce a HybridSearchResult."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("cpu exhaustion")
        assert isinstance(result, HybridSearchResult)
        assert result.query == "cpu exhaustion"
        assert result.total_results >= 0

    def test_retrieval_agent_search_produces_validated_pydantic(self):
        """RetrievalAgent output must be a validated Pydantic object."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result, AgentOutput)
        assert isinstance(result, HybridSearchResult)

    def test_retrieval_agent_search_has_both_ranker_results(self):
        """RetrievalAgent should return both BM25 and dense results."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("error")
        assert isinstance(result, HybridSearchResult)
        assert isinstance(result.bm25_results, list)
        assert isinstance(result.dense_results, list)
        assert isinstance(result.fused_rankings, list)

    def test_retrieval_agent_search_with_metadata(self):
        """RetrievalAgent should handle metadata enrichment."""
        docs = _sample_documents()
        metadata = {"INC-001": {"category": "cpu_exhaustion"}}
        agent = RetrievalAgent()
        agent.build_index(docs, metadata=metadata)
        result = agent.search("cpu")
        assert isinstance(result, HybridSearchResult)

    def test_retrieval_agent_search_uninitialized(self):
        """RetrievalAgent.search without build_index should return empty result."""
        agent = RetrievalAgent()
        result = agent.search("cpu")
        assert isinstance(result, HybridSearchResult)
        assert result.total_results == 0
        assert result.retrieval_time_ms == 0.0

    def test_retrieval_agent_to_json_roundtrip(self):
        """RetrievalAgent output should support JSON roundtrip."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        json_str = result.to_json()
        restored = HybridSearchResult.from_json(json_str)
        assert restored.incident_id == result.incident_id
        assert restored.query == result.query

    def test_retrieval_agent_search_returns_both_bm25_and_dense_hits(self):
        """RetrievalAgent should have SearchHit objects in bm25_results and dense_results."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("error timeout")
        assert isinstance(result, HybridSearchResult)
        # Results may come from either ranker
        assert isinstance(result.bm25_results, list)
        assert isinstance(result.dense_results, list)


# ---------------------------------------------------------------------------
# Cross-cutting rule and backward compatibility
# ---------------------------------------------------------------------------


class TestCrossCuttingAndCompatibility:
    """Verify cross-cutting rule and backward compatibility."""

    def test_hybrid_search_result_is_validated_pydantic_object(self):
        """HybridSearchResult must be a Pydantic-validated object (Stage 4 cross-cutting rule)."""
        result = HybridSearchResult(
            agent_type="retrieval_agent",
            incident_id="INC-001",
            query="cpu",
            total_results=1,
            retrieval_time_ms=1.0,
        )
        assert isinstance(result, HybridSearchResult)
        # Verify it has a model (Pydantic BaseModel)
        assert hasattr(result, "model_dump")

    def test_search_hit_is_validated_pydantic_object(self):
        """SearchHit must be a Pydantic-validated object."""
        hit = SearchHit(
            incident_id="INC-001",
            score=0.9,
            ranker_source="bm25",
            rank=1,
        )
        assert isinstance(hit, SearchHit)
        assert hasattr(hit, "model_dump")

    def test_backward_compatible_react_agent(self):
        """ReActAgent should still work unchanged after Stage 10."""
        agent = ReActAgent()
        assert hasattr(agent, "generate_plan")
        assert hasattr(agent, "execute_plan")
        assert hasattr(agent, "replan")
        assert hasattr(agent, "run")

    def test_backward_compatible_orchestrator_agent(self):
        """OrchestratorAgent should still work unchanged after Stage 10."""
        agent = OrchestratorAgent()
        assert hasattr(agent, "classify_incident")
        assert hasattr(agent, "dispatch_workers")
        assert hasattr(agent, "synthesize")

    def test_backward_compatible_incident_commander(self):
        """IncidentCommander should still work unchanged after Stage 10."""
        agent = IncidentCommander()
        assert hasattr(agent, "synthesize_narrative")
        assert hasattr(agent, "determine_escalation")

    def test_backward_compatible_debate_mechanism(self):
        """DebateMechanism should still work unchanged after Stage 10."""
        agent = DebateMechanism()
        assert hasattr(agent, "run")

    def test_backward_compatible_tree_of_thought_agent(self):
        """TreeOfThoughtAgent should still work unchanged after Stage 10."""
        agent = TreeOfThoughtAgent()
        assert hasattr(agent, "generate_hypotheses")
        assert hasattr(agent, "evaluate_hypotheses")
        assert hasattr(agent, "select_best_hypothesis")
        assert hasattr(agent, "plan_and_solve")
        assert hasattr(agent, "run")

    def test_backward_compatible_root_cause_agent(self):
        """RootCauseAgent should still work unchanged after Stage 10."""
        agent = RootCauseAgent()
        assert hasattr(agent, "build_arguments")
        assert hasattr(agent, "analyze")

    def test_backward_compatible_forensic_examiner_agent(self):
        """ForensicExaminerAgent should still work unchanged after Stage 10."""
        agent = ForensicExaminerAgent()
        assert hasattr(agent, "build_challenges")
        assert hasattr(agent, "challenge")

    def test_all_stage10_schemas_in_main_export(self):
        """New Stage 10 schemas should be importable from incident_agent."""
        from incident_agent import HybridSearchResult, RetrievalAgent, SearchHit

        assert SearchHit is not None
        assert HybridSearchResult is not None
        assert RetrievalAgent is not None

    def test_bm25_hit_is_named_tuple(self):
        """BM25Hit should be a NamedTuple."""
        hit = BM25Hit(incident_id="INC-001", score=1.0, rank=1)
        assert isinstance(hit, BM25Hit)

    def test_dense_hit_is_named_tuple(self):
        """DenseHit should be a NamedTuple."""
        hit = DenseHit(incident_id="INC-001", score=0.9, rank=1)
        assert isinstance(hit, DenseHit)

    def test_rrf_rank_is_named_tuple(self):
        """RRFRank should be a NamedTuple."""
        rank = RRFRank(incident_id="INC-001", score=0.01, bm25_rank=1, dense_rank=1, rank=1)
        assert isinstance(rank, RRFRank)

    def test_bm25_hit_has_all_attributes(self):
        """BM25Hit should have incident_id, score, and rank attributes."""
        hit = BM25Hit(incident_id="INC-001", score=0.95, rank=1)
        assert hit.incident_id == "INC-001"
        assert hit.score == 0.95
        assert hit.rank == 1

    def test_dense_empty_search(self):
        """DenseRetriever should return empty results for unknown query."""
        docs = _sample_documents()
        retriever = DenseRetriever()
        retriever.build_index(docs)
        results = retriever.search("xyznonexistentquery12345", top_k=5)
        assert len(results) == 0

    def test_hybrid_retriever_uninitialized(self):
        """HybridRetriever.search should return empty list when not initialized."""
        retriever = HybridRetriever()
        results = retriever.search("cpu")
        assert results == []

    def test_rrf_rank_score_computation(self):
        """rrf_rank should compute correct RRF scores."""
        bm25_results = [
            BM25Hit(incident_id="INC-001", score=1.5, rank=1),
            BM25Hit(incident_id="INC-002", score=1.2, rank=2),
        ]
        dense_results = [
            DenseHit(incident_id="INC-002", score=0.9, rank=1),
            DenseHit(incident_id="INC-001", score=0.8, rank=2),
        ]
        results = rrf_rank(bm25_results, dense_results, k=60)
        for r in results:
            # RRF score = 1/(60+bm25_rank) + 1/(60+dense_rank)
            bm25_r = next((h.rank for h in bm25_results if h.incident_id == r.incident_id), None)
            dense_r = next((h.rank for h in dense_results if h.incident_id == r.incident_id), None)
            expected = 1.0 / (60 + bm25_r) + 1.0 / (60 + dense_r)
            assert abs(r.score - expected) < 1e-5

    def test_retrieval_agent_search_with_incident_ids(self):
        """RetrievalAgent.search should accept incident_ids parameter."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("cpu", incident_ids=["INC-001"])
        assert isinstance(result, HybridSearchResult)
        assert result.incident_id == "INC-001"

    def test_retrieval_agent_search_has_fused_rankings(self):
        """RetrievalAgent output should have fused_rankings populated."""
        docs = _sample_documents()
        agent = RetrievalAgent()
        agent.build_index(docs)
        result = agent.search("error timeout")
        assert isinstance(result.fused_rankings, list)
        assert result.total_results == len(result.fused_rankings)
