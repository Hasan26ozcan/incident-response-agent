"""Tests for Stage 11 — Cross-Encoder Re-ranking.

Covers:
  - RelevanceDelta schema validation.
  - RerankedResult schema validation, to_json/from_json roundtrip, format_report.
  - CrossEncoderReranker: index building, cross-encoder scoring, rerank.
  - RerankerAgent: full pipeline producing validated RerankedResult.
  - Before/after MRR and Precision@K comparison metrics.
  - Cross-cutting rule: RerankedResult is a validated Pydantic object.
  - Backward compatibility: all prior agents still work unchanged.
"""

from __future__ import annotations

import pytest

from incident_agent.agents.debate_mechanism import DebateMechanism
from incident_agent.agents.forensic_examiner_agent import ForensicExaminerAgent
from incident_agent.agents.incident_commander import IncidentCommander
from incident_agent.agents.orchestrator import OrchestratorAgent
from incident_agent.agents.react_agent import ReActAgent
from incident_agent.agents.reranker_agent import RerankerAgent
from incident_agent.agents.root_cause_agent import RootCauseAgent
from incident_agent.agents.tree_of_thought_agent import TreeOfThoughtAgent
from incident_agent.retrieval.reranker import CrossEncoderReranker
from incident_agent.schemas import (
    RelevanceDelta,
    RerankedResult,
)
from incident_agent.schemas.agent_output import AgentOutput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_documents() -> dict[str, str]:
    """Return sample incident text documents."""
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
# RelevanceDelta schema
# ---------------------------------------------------------------------------


class TestRelevanceDeltaSchema:
    """Verify RelevanceDelta is a properly structured Pydantic model."""

    def test_relevance_delta_is_pydantic_model(self):
        """RelevanceDelta should be a Pydantic BaseModel subclass."""
        delta = RelevanceDelta(
            incident_id="INC-001",
            rrf_score=0.5,
            cross_encoder_score=0.7,
            delta=0.2,
            rank_change=-1,
        )
        assert isinstance(delta, RelevanceDelta)
        assert delta.incident_id == "INC-001"

    def test_relevance_delta_incident_id_pattern(self):
        """RelevanceDelta incident_id must match INC-XXX pattern."""
        with pytest.raises(Exception):
            RelevanceDelta(
                incident_id="INVALID",
                rrf_score=0.5,
                cross_encoder_score=0.5,
                delta=0.0,
                rank_change=0,
            )

    def test_relevance_delta_to_json_roundtrip(self):
        """RelevanceDelta serialization should roundtrip."""
        delta = RelevanceDelta(
            incident_id="INC-001",
            rrf_score=0.5,
            cross_encoder_score=0.7,
            delta=0.2,
            rank_change=-1,
        )
        json_str = delta.model_dump_json()
        restored = RelevanceDelta.model_validate_json(json_str)
        assert restored.incident_id == delta.incident_id


# ---------------------------------------------------------------------------
# RerankedResult schema
# ---------------------------------------------------------------------------


class TestRerankedResultSchema:
    """Verify RerankedResult is a properly structured Pydantic model."""

    def test_reranked_result_is_pydantic_model(self):
        """RerankedResult should be a Pydantic BaseModel subclass."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            confidence=0.8,
            total_results=5,
        )
        assert isinstance(result, RerankedResult)
        assert result.total_results == 5

    def test_reranked_result_is_agent_output(self):
        """RerankedResult should inherit from AgentOutput."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            confidence=0.8,
            total_results=5,
        )
        assert isinstance(result, AgentOutput)

    def test_reranked_result_to_json_roundtrip(self):
        """RerankedResult serialization should roundtrip correctly."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            confidence=0.8,
            total_results=5,
        )
        json_str = result.model_dump_json()
        restored = RerankedResult.model_validate_json(json_str)
        assert restored.incident_id == result.incident_id

    def test_reranked_result_format_report(self):
        """format_report should produce a non-empty string."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            confidence=0.8,
            total_results=1,
        )
        report = result.format_report()
        assert len(report) > 0
        assert "Reranking Report" in report

    def test_reranked_result_improvement_score(self):
        """improvement_score should be computable and bounded."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            confidence=0.8,
            total_results=5,
            mean_reciprocal_rank_before=0.5,
            mean_reciprocal_rank_after=0.7,
            precision_at_k_before=0.4,
            precision_at_k_after=0.6,
        )
        expected = (0.7 + 0.6) - (0.5 + 0.4)
        assert result.improvement_score == expected


# ---------------------------------------------------------------------------
# CrossEncoderReranker
# ---------------------------------------------------------------------------


class TestCrossEncoderReranker:
    """Verify CrossEncoderReranker works correctly."""

    def test_reranker_is_initialized(self):
        """CrossEncoderReranker should be instantiable."""
        reranker = CrossEncoderReranker()
        assert reranker.top_k_candidates == 20
        assert reranker._initialized is False

    def test_reranker_build_index(self):
        """CrossEncoderReranker should build an index from documents."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        assert reranker._initialized is True
        assert len(reranker._vocabulary) > 0

    def test_reranker_rerank_returns_results(self):
        """CrossEncoderReranker.rerank should return results."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        candidate_ids = list(docs.keys())
        results = reranker.rerank("cpu exhaustion", candidate_ids, docs)
        assert isinstance(results, list)

    def test_reranker_returns_sorted_by_score(self):
        """Re-ranked results should be sorted by descending cross-encoder score."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        candidate_ids = list(docs.keys())
        results = reranker.rerank("cpu", candidate_ids, docs)
        for i in range(len(results) - 1):
            assert results[i][1] >= results[i + 1][1]

    def test_reranker_returns_correct_tuple_format(self):
        """Each result should be a (incident_id, score, rank) tuple."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        candidate_ids = list(docs.keys())
        results = reranker.rerank("cpu", candidate_ids, docs, top_k=3)
        for incident_id, score, rank in results:
            assert isinstance(incident_id, str)
            assert isinstance(score, float)
            assert isinstance(rank, int)
            assert rank >= 1

    def test_reranker_respects_top_k(self):
        """rerank should respect top_k limit."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        candidate_ids = list(docs.keys())
        results = reranker.rerank("cpu", candidate_ids, docs, top_k=3)
        assert len(results) <= 3

    def test_reranker_empty_candidates(self):
        """rerank should return empty list for empty candidates."""
        reranker = CrossEncoderReranker()
        results = reranker.rerank("cpu", [], _sample_documents())
        assert results == []

    def test_reranker_uninitialized(self):
        """rerank should return empty list when not initialized."""
        reranker = CrossEncoderReranker()
        results = reranker.rerank("cpu", ["INC-001"], _sample_documents())
        assert results == []

    def test_reranker_score_is_non_negative(self):
        """Cross-encoder scores should be non-negative."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        candidate_ids = list(docs.keys())
        results = reranker.rerank("cpu", candidate_ids, docs)
        for _, score, _ in results:
            assert score >= 0.0

    def test_search_delegates_to_rerank(self):
        """search should return re-ranked results from the candidate pool."""
        docs = _sample_documents()
        reranker = CrossEncoderReranker()
        reranker.build_index(docs)
        candidate_ids = list(docs.keys())
        results = reranker.search("cpu", candidate_ids, docs)
        assert isinstance(results, list)
        if results:
            assert isinstance(results[0][0], str)


# ---------------------------------------------------------------------------
# RerankerAgent
# ---------------------------------------------------------------------------


class TestRerankerAgent:
    """Verify RerankerAgent works correctly."""

    def test_reranker_agent_is_initialized(self):
        """RerankerAgent should be instantiable."""
        agent = RerankerAgent()
        assert agent.top_k_rerank == 10
        assert agent.reranker.top_k_candidates == 20

    def test_reranker_agent_build_index(self):
        """RerankerAgent should build index from documents."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        assert agent._initialized is True

    def test_reranker_agent_search_returns_reranked_result(self):
        """RerankerAgent.search should produce a RerankedResult."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu exhaustion")
        assert isinstance(result, RerankedResult)
        assert result.query == "cpu exhaustion"

    def test_reranker_agent_search_produces_validated_pydantic(self):
        """RerankerAgent output must be a validated Pydantic object."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result, AgentOutput)
        assert isinstance(result, RerankedResult)

    def test_reranker_agent_search_has_pre_post_rankings(self):
        """RerankerAgent should have both pre and post rerank rankings."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("error")
        assert isinstance(result, RerankedResult)
        assert isinstance(result.pre_rerank_rankings, list)
        assert isinstance(result.post_rerank_rankings, list)

    def test_reranker_agent_search_has_relevance_deltas(self):
        """RerankerAgent should compute relevance deltas."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result.relevance_deltas, list)

    def test_reranker_agent_search_has_metrics(self):
        """RerankerAgent should compute MRR and Precision@K."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("error timeout")
        assert isinstance(result, RerankedResult)
        assert 0.0 <= result.mean_reciprocal_rank_before <= 1.0
        assert 0.0 <= result.mean_reciprocal_rank_after <= 1.0
        assert 0.0 <= result.precision_at_k_before <= 1.0
        assert 0.0 <= result.precision_at_k_after <= 1.0

    def test_reranker_agent_improvement_score(self):
        """improvement_score should reflect after vs before metrics."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result, RerankedResult)
        assert isinstance(result.improvement_score, float)

    def test_reranker_agent_search_with_incident_ids(self):
        """RerankerAgent.search should accept incident_ids parameter."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu", incident_ids=["INC-001"])
        assert isinstance(result, RerankedResult)

    def test_reranker_agent_search_uninitialized(self):
        """RerankerAgent.search without build_index should return empty result."""
        agent = RerankerAgent()
        result = agent.search("cpu")
        assert isinstance(result, RerankedResult)
        assert result.total_results == 0

    def test_reranker_agent_to_json_roundtrip(self):
        """RerankerAgent output should support JSON roundtrip."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        json_str = result.to_json()
        restored = RerankedResult.from_json(json_str)
        assert restored.incident_id == result.incident_id
        assert restored.query == result.query

    def test_reranker_agent_search_has_format_report(self):
        """RerankedResult should produce a format_report."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        report = result.format_report()
        assert len(report) > 0
        assert "Reranking Report" in report

    def test_reranker_agent_search_has_both_ranker_sources(self):
        """Pre-rankings should have rrf source, post should have cross_encoder."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("error")
        assert isinstance(result, RerankedResult)
        if result.pre_rerank_rankings:
            assert result.pre_rerank_rankings[0].ranker_source == "rrf"
        if result.post_rerank_rankings:
            assert result.post_rerank_rankings[0].ranker_source == "cross_encoder"

    def test_reranker_agent_search_relevance_delta_fields(self):
        """RelevanceDelta objects should have all required fields."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result, RerankedResult)
        for delta in result.relevance_deltas[:3]:
            assert isinstance(delta.incident_id, str)
            assert isinstance(delta.rrf_score, float)
            assert isinstance(delta.cross_encoder_score, float)
            assert isinstance(delta.delta, float)
            assert isinstance(delta.rank_change, int)

    def test_reranker_agent_search_reranking_time(self):
        """reranking_time_ms should be present."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result.reranking_time_ms, float)
        assert result.reranking_time_ms >= 0.0


# ---------------------------------------------------------------------------
# Cross-cutting rule and backward compatibility
# ---------------------------------------------------------------------------


class TestCrossCuttingAndCompatibility:
    """Verify cross-cutting rule and backward compatibility."""

    def test_reranked_result_is_validated_pydantic_object(self):
        """RerankedResult must be a Pydantic-validated object."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu",
            confidence=0.8,
            total_results=1,
        )
        assert isinstance(result, RerankedResult)
        assert hasattr(result, "model_dump")

    def test_relevance_delta_is_validated_pydantic_object(self):
        """RelevanceDelta must be a Pydantic-validated object."""
        delta = RelevanceDelta(
            incident_id="INC-001",
            rrf_score=0.5,
            cross_encoder_score=0.7,
            delta=0.2,
            rank_change=-1,
        )
        assert isinstance(delta, RelevanceDelta)
        assert hasattr(delta, "model_dump")

    def test_reranked_result_is_agent_output(self):
        """RerankedResult should be an AgentOutput (Stage 4 cross-cutting rule)."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu",
            confidence=0.8,
            total_results=1,
        )
        assert isinstance(result, AgentOutput)

    def test_all_stage11_schemas_in_main_export(self):
        """New Stage 11 schemas should be importable from incident_agent."""
        from incident_agent import RelevanceDelta, RerankedResult, RerankerAgent

        assert RerankedResult is not None
        assert RerankerAgent is not None
        assert RelevanceDelta is not None

    def test_backward_compatible_react_agent(self):
        """ReActAgent should still work unchanged after Stage 11."""
        agent = ReActAgent()
        assert hasattr(agent, "generate_plan")
        assert hasattr(agent, "execute_plan")
        assert hasattr(agent, "replan")
        assert hasattr(agent, "run")

    def test_backward_compatible_orchestrator_agent(self):
        """OrchestratorAgent should still work unchanged after Stage 11."""
        agent = OrchestratorAgent()
        assert hasattr(agent, "classify_incident")
        assert hasattr(agent, "dispatch_workers")
        assert hasattr(agent, "synthesize")

    def test_backward_compatible_incident_commander(self):
        """IncidentCommander should still work unchanged after Stage 11."""
        agent = IncidentCommander()
        assert hasattr(agent, "synthesize_narrative")
        assert hasattr(agent, "determine_escalation")

    def test_backward_compatible_debate_mechanism(self):
        """DebateMechanism should still work unchanged after Stage 11."""
        agent = DebateMechanism()
        assert hasattr(agent, "run")

    def test_backward_compatible_tree_of_thought_agent(self):
        """TreeOfThoughtAgent should still work unchanged after Stage 11."""
        agent = TreeOfThoughtAgent()
        assert hasattr(agent, "generate_hypotheses")
        assert hasattr(agent, "evaluate_hypotheses")
        assert hasattr(agent, "select_best_hypothesis")
        assert hasattr(agent, "plan_and_solve")
        assert hasattr(agent, "run")

    def test_backward_compatible_root_cause_agent(self):
        """RootCauseAgent should still work unchanged after Stage 11."""
        agent = RootCauseAgent()
        assert hasattr(agent, "build_arguments")
        assert hasattr(agent, "analyze")

    def test_backward_compatible_forensic_examiner_agent(self):
        """ForensicExaminerAgent should still work unchanged after Stage 11."""
        agent = ForensicExaminerAgent()
        assert hasattr(agent, "build_challenges")
        assert hasattr(agent, "challenge")

    def test_backward_compatible_retrieval_agent(self):
        """RetrievalAgent should still work unchanged after Stage 11."""
        from incident_agent.agents.retrieval_agent import RetrievalAgent

        agent = RetrievalAgent()
        assert hasattr(agent, "build_index")
        assert hasattr(agent, "search")

    def test_cross_encoder_reranker_in_retrieval_init(self):
        """CrossEncoderReranker should be importable from retrieval package."""
        from incident_agent.retrieval import CrossEncoderReranker

        assert CrossEncoderReranker is not None

    def test_reranked_result_to_from_json_roundtrip(self):
        """RerankedResult JSON roundtrip should work."""
        result = RerankedResult(
            agent_type="reranker_agent",
            incident_id="INC-001",
            query="cpu exhaustion",
            confidence=0.8,
            total_results=5,
            mean_reciprocal_rank_before=0.5,
            mean_reciprocal_rank_after=0.7,
            precision_at_k_before=0.4,
            precision_at_k_after=0.6,
        )
        json_str = result.to_json()
        restored = RerankedResult.from_json(json_str)
        assert restored.incident_id == result.incident_id
        assert restored.query == result.query
        assert restored.mean_reciprocal_rank_after == result.mean_reciprocal_rank_after

    def test_reranker_agent_uses_retrieval_agent(self):
        """RerankerAgent should use RetrievalAgent internally for hybrid results."""

        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("error")
        # The result should contain pre_rerank_rankings from RetrievalAgent
        assert isinstance(result.pre_rerank_rankings, list)
        # And post_rerank_rankings from cross-encoder
        assert isinstance(result.post_rerank_rankings, list)

    def test_reranker_rerank_produces_relevance_deltas(self):
        """Re-ranking should produce meaningful relevance deltas."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("cpu")
        assert isinstance(result, RerankedResult)
        # Verify deltas list is populated
        assert len(result.relevance_deltas) > 0

    def test_reranker_improvement_score_can_be_positive(self):
        """improvement_score should reflect before/after comparison."""
        docs = _sample_documents()
        agent = RerankerAgent()
        agent.build_index(docs)
        result = agent.search("memory")
        assert isinstance(result, RerankedResult)
        assert isinstance(result.improvement_score, float)
        # Verify the score is computed correctly
        expected = (result.mean_reciprocal_rank_after + result.precision_at_k_after) - (
            result.mean_reciprocal_rank_before + result.precision_at_k_before
        )
        assert abs(result.improvement_score - expected) < 1e-5
