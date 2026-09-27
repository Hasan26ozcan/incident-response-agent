# Proposal: Stage 10 — Vector DB & Hybrid Retrieval

- **Status:** Approved
- **Stage:** 10 of 23
- **Phase:** D — Memory & Retrieval
- **Depends on:** Stage 9 (Tree-of-Thought + Plan-and-Solve)

## 1. Problem / Motivation

At Stage 9, the Tree-of-Thought agent generates and evaluates alternative root-cause hypotheses, but it has no access to historical incident knowledge. The agent reasons purely from the current incident's data without leveraging patterns from past incidents.

Stage 10 introduces the **Vector DB & Hybrid Retrieval** layer: a knowledge-grounded retrieval system that stores past incident data as vector embeddings and keyword indexes, enabling agents to retrieve historically similar incidents as evidence during diagnosis. This combines:

- **BM25** keyword retrieval for precise log/error message matching
- **Dense** TF-IDF cosine-similarity retrieval for semantic similarity
- **RRF (Reciprocal Rank Fusion)** to fuse both ranking signals into a unified result set

This is particularly important for complex incidents where historical patterns provide strong evidence — the agent can identify "this looks like the same pattern as INC-003" and leverage the previously validated root cause.

## 2. Scope

### In scope

- **`SearchHit` Pydantic model**:
  - `incident_id`, `score`, `ranker_source`, `rank`, `metadata`
  - All fields validated by Pydantic (Stage 4 cross-cutting rule)
- **`HybridSearchResult` Pydantic model**:
  - Extends `AgentOutput` with `query`, `bm25_results`, `dense_results`, `rrf_scores`, `fused_rankings`, `total_results`, `retrieval_time_ms`
  - All fields validated by Pydantic
- **`BM25Retriever` class**:
  - `build_index(documents)` — builds inverted index from incident text
  - `search(query, top_k)` — BM25+ keyword retrieval
  - Default `k1=1.5`, `b=0.75`
- **`DenseRetriever` class**:
  - `build_index(documents)` — builds TF-IDF matrix using numpy
  - `search(query, top_k)` — cosine-similarity retrieval
- **`rrf_rank(bm25_results, dense_results, k=60)` function**:
  - Reciprocal Rank Fusion combining both rankers
- **`HybridRetriever` class**:
  - Orchestrates BM25 + Dense → RRF → fused results
- **`RetrievalAgent` class**:
  - `build_index(documents, metadata)` — builds full hybrid index
  - `search(query, incident_ids)` — full pipeline producing `HybridSearchResult`
- **Cross-cutting rule maintained**: every agent output is a validated Pydantic object (Stage 4 requirement)
- **Test suite**: comprehensive tests for all retrieval components, schema validation, and backward compatibility

### Out of scope

- Qdrant server deployment (local/in-memory mode used)
- LLM-based query expansion
- Real-time index updates (batch indexing at startup)
- Permanent vector storage (all data in memory)
- Reranking with cross-encoder (Stage 11)
- Episodic memory (Stage 12)

## 3. Design decisions

### 3.1 qdrant-client in local/in-memory mode

**Decision**: Use `qdrant-client` library for API compatibility and future server deployment, but operate in local in-memory mode for Stage 10.

**Rationale**:
- Provides a clean vector DB API without requiring a running Qdrant server
- Can be migrated to a persistent Qdrant instance in later stages
- Maintains the dependency pattern used throughout the project
- The in-memory mode is deterministic and testable

### 3.2 BM25 implemented from scratch

**Decision**: BM25+ scoring is implemented using only the standard library (no external dependency).

**Rationale**:
- BM25 is a well-understood algorithm with simple implementation
- Avoids adding an extra dependency for a deterministic algorithm
- Full control over the scoring formula (BM25+ variant)
- Works consistently with the project's numpy dependency

### 3.3 Dense retrieval uses numpy TF-IDF

**Decision**: TF-IDF vectors are computed using numpy rather than a transformer model.

**Rationale**:
- numpy is already a project dependency (used for torch)
- TF-IDF provides adequate semantic similarity for the synthetic dataset
- No GPU required, keeping the test suite fast and deterministic
- Transformer-based embeddings could be added in later stages

### 3.4 RRF as fusion strategy

**Decision**: Reciprocal Rank Fusion (RRF) is used to combine BM25 and dense rankings.

**Rationale**:
- RRF is rank-based and does not require score normalization
- Formula: `score(d) = sum(1/(k + rank))` where k is typically 60
- Handles different ranking scales gracefully
- Widely adopted in information retrieval (Microsoft Research)
- Computationally lightweight

### 3.5 RetrievalAgent produces validated Pydantic output

**Decision**: The `RetrievalAgent.search()` method returns a `HybridSearchResult` (Pydantic model).

**Rationale**:
- Maintains the Stage 4 cross-cutting rule: every agent output is a validated Pydantic object
- Enables downstream agents to access structured retrieval results
- Supports JSON serialization for inter-agent communication

## 4. Schema definitions

### 4.1 SearchHit

```python
class SearchHit(BaseModel):
    incident_id: str          # INC-XXX format
    score: float              # Relevance score (>= 0.0)
    ranker_source: str        # "bm25", "dense", or "rrf"
    rank: int                 # 1-based rank position
    metadata: dict            # Additional result metadata
```

### 4.2 HybridSearchResult

```python
class HybridSearchResult(AgentOutput):
    incident_id: str
    query: str
    bm25_results: list[SearchHit]
    dense_results: list[SearchHit]
    rrf_scores: dict[str, float]
    fused_rankings: list[SearchHit]
    total_results: int
    retrieval_time_ms: float
```

### 4.3 Retrieval pipeline

```
Input: query string
  → BM25Retriever.search()      → BM25Hit[]
  → DenseRetriever.search()     → DenseHit[]
  → rrf_rank()                  → RRFRank[] (fused)
  → RetrievalAgent.search()     → HybridSearchResult
Output: HybridSearchResult
```

## 5. Acceptance criteria

- [x] `SearchHit` Pydantic model with incident_id, score, ranker_source, rank, metadata
- [x] `HybridSearchResult` Pydantic model extending `AgentOutput`
- [x] All fields validated by Pydantic (Stage 4 cross-cutting rule)
- [x] `BM25Retriever` class with `build_index()` and `search()` methods
- [x] `DenseRetriever` class with `build_index()` and `search()` methods
- [x] `rrf_rank()` function fusing BM25 and dense rankings
- [x] `HybridRetriever` class orchestrating BM25 + Dense → RRF
- [x] `RetrievalAgent` class with `build_index()` and `search()` methods
- [x] `RetrievalAgent.search()` produces a schema-valid `HybridSearchResult`
- [x] BM25 returns relevant keyword matches
- [x] Dense retrieval returns semantically similar results
- [x] RRF fused ranking combines both signals
- [x] `HybridSearchResult.to_json()` and `from_json()` work correctly
- [x] `HybridSearchResult.format_report()` produces human-readable report
- [x] Cross-cutting rule verified: `HybridSearchResult` is a validated Pydantic object
- [x] `SearchHit` is a validated Pydantic object
- [x] All tests pass (Stage 10 + existing stages)
- [x] Backward compatible: `ReActAgent`, `OrchestratorAgent`, `IncidentCommander`, `DebateMechanism`, `TreeOfThoughtAgent`, `RootCauseAgent`, `ForensicExaminerAgent` still work unchanged
- [x] `ruff check`, `ruff format`, `mypy`, `bandit` — all green

## 6. Risks / open questions

- **qdrant-client in-memory mode**: Using qdrant-client without a server requires local in-memory mode. The client API is compatible with future server deployment if needed.
- **TF-IDF quality**: Numpy TF-IDF is simpler than transformer embeddings but may miss semantic nuance. For the synthetic dataset, keyword and TF-IDF approaches are sufficient.
- **Index build time**: Building BM25 + Dense indexes at startup has a one-time cost. For 20 incidents this is negligible (<100ms).
- **Retrieval quality**: BM25 and dense are complementary. RRF fusion provides robustness: if one ranker misses a relevant result, the other may catch it.
- **Circular imports**: `retrieval_agent.py` imports from `retrieval.hybrid` and `schemas.vector_search`. No circular dependencies as the schemas do not import from agents.
