# Proposal: Stage 11 — Reranking

- **Status:** Approved
- **Stage:** 11 of 23
- **Phase:** D — Memory & Retrieval
- **Depends on:** Stage 10 (Vector DB & Hybrid Retrieval)

## 1. Problem / Motivation

At Stage 10, the hybrid retrieval system combines BM25 keyword matching, dense TF-IDF cosine similarity, and Reciprocal Rank Fusion (RRF) to produce a unified ranked result set. However, RRF is a **rank-based** fusion strategy: each ranker independently ranks documents, and RRF combines those rankings without ever considering how well a query-document pair matches as a whole.

A **cross-encoder** approach addresses this limitation by scoring each (query, document) pair **jointly** — the model takes both the query and document as input simultaneously and produces a single relevance score. This is fundamentally more powerful than bi-encoder approaches (like BM25 and TF-IDF) because it captures query-document interactions directly.

Stage 11 introduces the **Cross-Encoder Re-ranker**: it takes the top-N candidates from the Stage 10 hybrid retriever and re-scores them using a cross-encoder that models query-document interactions. The result is a before/after comparison showing retrieval quality improvement.

## 2. Scope

### In scope

- **`RelevanceDelta` Pydantic model**:
  - `incident_id`, `rrf_score`, `cross_encoder_score`, `delta`, `rank_change`
  - Tracks per-document relevance change after reranking
  - All fields validated by Pydantic (Stage 4 cross-cutting rule)

- **`RerankedResult` Pydantic model**:
  - Extends `AgentOutput` with `query`, `pre_rerank_rankings`, `post_rerank_rankings`
  - `relevance_deltas`, `mean_reciprocal_rank_before/after`, `precision_at_k_before/after`
  - `improvement_score`, `reranking_time_ms`
  - All fields validated by Pydantic
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods

- **`CrossEncoderReranker` class**:
  - `build_index(documents)` — builds TF-IDF vocabulary and BM25 baseline
  - `rerank(query, candidate_ids, documents, top_k)` — re-scores candidates jointly
  - Deterministic MLP with fixed weights (no training required)
  - Interaction features simulating cross-attention between query and document

- **`RerankerAgent` class**:
  - `build_index(documents, metadata)` — builds full reranker index
  - `search(query, incident_ids)` — full pipeline producing `RerankedResult`
  - Pipeline: hybrid retrieval → candidate extraction → cross-encoder reranking → metrics

- **Retrieval quality metrics**:
  - Mean Reciprocal Rank (MRR) before and after reranking
  - Precision@K before and after reranking
  - `improvement_score` measuring net quality gain

- **Cross-cutting rule maintained**: every agent output is a validated Pydantic object (Stage 4 requirement)
- **Test suite**: comprehensive tests for all reranking components, schema validation, and backward compatibility

### Out of scope

- Actual transformer-based cross-encoder models (e.g., cross-encoder/ms-marco-MiniLM-L-12-v2)
- Qdrant server deployment (still local/in-memory mode)
- Permanent vector storage
- Episodic memory with self-improvement (Stage 12)
- MCP servers (Stage 13)

## 3. Design decisions

### 3.1 Deterministic cross-encoder simulation

**Decision**: Implement the cross-encoder as a deterministic MLP with fixed weights over TF-IDF interaction features, rather than using a pre-trained transformer model.

**Rationale**:
- The project maintains a deterministic, template-based architecture throughout (no LLM calls for retrieval)
- Adding a transformer model would introduce significant dependencies and non-determinism
- The deterministic simulation preserves the project's testability and reproducibility
- The interaction features (element-wise product, BM25 baseline) capture the essence of cross-attention
- Can be replaced with a real cross-encoder in a future stage without changing the API

### 3.2 Two-stage retrieval pipeline

**Decision**: Use the Stage 10 hybrid retriever as a first-pass candidate generator, then apply cross-encoder reranking only to the top-N candidates.

**Rationale**:
- Cross-encoding every document in the corpus would be computationally expensive
- BM25 + RRF already filters to a reasonable candidate pool (top 20)
- The cross-encoder only needs to re-rank 20 candidates, not thousands
- This "bi-encode then cross-encode" pattern is widely used in production retrieval systems
- Balances retrieval quality with computational efficiency

### 3.3 MRR and Precision@K as quality metrics

**Decision**: Use Mean Reciprocal Rank (MRR) and Precision@K to measure retrieval quality before and after reranking.

**Rationale**:
- MRR measures how quickly relevant documents appear in the ranking (inverse rank of first relevant item)
- Precision@K measures the fraction of top-K results that are relevant
- Both are standard information retrieval metrics
- Together they provide a comprehensive view of reranking effectiveness
- The `improvement_score` combines both metrics into a single comparison value

### 3.4 RerankedResult produces validated Pydantic output

**Decision**: The `RerankerAgent.search()` method returns a `RerankedResult` (Pydantic model).

**Rationale**:
- Maintains the Stage 4 cross-cutting rule: every agent output is a validated Pydantic object
- Enables downstream agents to access structured reranking results
- Supports JSON serialization for inter-agent communication
- Provides human-readable `format_report()` for debugging

## 4. Schema definitions

### 4.1 RelevanceDelta

```python
class RelevanceDelta(BaseModel):
    incident_id: str  # INC-XXX format
    rrf_score: float  # Original RRF fused score
    cross_encoder_score: float  # Cross-encoder reranked score
    delta: float  # cross_encoder_score - rrf_score
    rank_change: int  # Change in rank (negative = improved)
```

### 4.2 RerankedResult

```python
class RerankedResult(AgentOutput):
    query: str
    confidence: float
    pre_rerank_rankings: list[SearchHit]  # RRF rankings before
    post_rerank_rankings: list[SearchHit]  # Cross-encoder rankings after
    relevance_deltas: list[RelevanceDelta]
    mean_reciprocal_rank_before: float
    mean_reciprocal_rank_after: float
    precision_at_k_before: float
    precision_at_k_after: float
    improvement_score: float
    reranking_time_ms: float
    total_results: int
    retrieval_time_ms: float
```

### 4.3 CrossEncoderReranker pipeline

```
Input: query string, candidate_ids, documents
  → CrossEncoderReranker.build_index(documents)
  → CrossEncoderReranker.rerank(query, candidate_ids, documents, top_k)
    → For each (query, doc) pair:
      1. Compute TF-IDF vectors
      2. Compute BM25 baseline score
      3. Compute interaction features (element-wise product, BM25)
      4. Score through deterministic MLP
    → Sort by descending cross-encoder score
Output: list of (incident_id, cross_encoder_score, rank)
```

### 4.4 RerankerAgent pipeline

```
Input: query string, incident_ids (optional)
  → RetrievalAgent.search(query) → HybridSearchResult
  → Extract candidate_ids from fused_rankings
  → CrossEncoderReranker.rerank(query, candidate_ids, documents)
  → Compute MRR and Precision@K before/after
  → RerankerAgent.search() → RerankedResult
Output: RerankedResult with before/after comparison
```

## 5. Acceptance criteria

- [x] `RelevanceDelta` Pydantic model with incident_id, rrf_score, cross_encoder_score, delta, rank_change
- [x] `RerankedResult` Pydantic model extending `AgentOutput`
- [x] All fields validated by Pydantic (Stage 4 cross-cutting rule)
- [x] `CrossEncoderReranker` class with `build_index()` and `rerank()` methods
- [x] `RerankerAgent` class with `build_index()` and `search()` methods
- [x] `RerankerAgent.search()` produces a schema-valid `RerankedResult`
- [x] Cross-encoder scores query-document pairs jointly (not independently)
- [x] MRR and Precision@K computed before and after reranking
- [x] `improvement_score` reflects net quality gain
- [x] `RerankedResult.to_json()` and `from_json()` work correctly
- [x] `RerankedResult.format_report()` produces human-readable report
- [x] Cross-cutting rule verified: `RerankedResult` is a validated Pydantic object
- [x] `RelevanceDelta` is a validated Pydantic object
- [x] All tests pass (Stage 11 + existing stages)
- [x] Backward compatible: `ReActAgent`, `OrchestratorAgent`, `IncidentCommander`, `DebateMechanism`, `TreeOfThoughtAgent`, `RootCauseAgent`, `ForensicExaminerAgent`, `RetrievalAgent` still work unchanged
- [x] `ruff check`, `ruff format`, `mypy`, `bandit` — all green

## 6. Risks / open questions

- **Deterministic MLP quality**: The fixed-weight MLP is a simulation, not a trained model. Re-ranking quality depends on feature engineering rather than learned weights. For the synthetic dataset, feature-based scoring provides meaningful re-ranking.
- **Candidate pool size**: Using top 20 candidates from RRF may miss relevant documents that neither BM25 nor dense ranked highly. A larger candidate pool could improve coverage but increases computation.
- **Cross-encoder vs. bi-encoder**: The fundamental question of whether joint scoring always outperforms rank-based fusion depends on the dataset. The comparison report quantifies this empirically.
- **Computational cost**: Cross-encoding each candidate pair is more expensive than RRF. For larger document collections, this could become a bottleneck. The two-stage approach mitigates this.
- **Circular imports**: `reranker_agent.py` imports from `retrieval_agent` inside the `search()` method to avoid circular imports. `reranker.py` depends only on numpy and the standard library.
