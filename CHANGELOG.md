# Changelog

All notable changes to the Incident Response Agent project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Stage 14 — MCP Discovery & Resource Lifecycle] — 2026-10
### Added
- Every agent-facing server now also exposes read-only resources (`incident://<id>/meta`, `logs/overview`, `metrics/summary`, `deploys/recent`, `diagnostics`) whose text is the same as the matching tool output (tested for all 28 incidents). Tools are unchanged.
- `discovery.py`: `take_snapshot` (server name, instructions, negotiated protocol version, tools with schemas, resources) and the pure function `negotiate(snapshots, Needs)`: required tools first, optional tools in order, a tool budget for the agent's context, reports missing and dropped tools, name conflicts (first server wins) and servers that cannot push changes.
- `hub.py` (`McpHub`): several connections, each in its own task; idempotent `close`, `reconnect`, failed-connect state, everything closed when the `async with` ends (also on errors); tool calls routed by the discovered catalog; cached resource reads; live updates through `subscriptions/listen` (resource updated -> cache entry dropped, tools changed -> catalog refreshed, resources changed -> catalog refreshed and stale cache entries deleted); dropped streams are re-opened a limited number of times, then TTL-only.
- `resources.py` (`ResourceCache`): TTL + LRU, `invalidate`, `drop_server`, `keep_only`, `sweep`, injectable clock.
- `live.py` + `demo.py`: a simulated live source (feed update, tool appears, source decommissioned) and `python -m incident_agent.mcp_servers.demo`.
- Tests: `test_mcp_discovery.py` (63 tests, mutation-checked, includes failure injection with a fake client and a real stdio round trip).
### Notes
`resources/subscribe` no longer exists in MCP 2026-07-28 (the SDK answers "Method not found" although the capabilities still say `subscribe=True`); `Client.listen()` is used instead. There is no replay on that stream, so `open()` waits for the stream to be acknowledged and a lost stream drops the server's cache entries. The SDK has no public way to remove a resource at runtime; `live.py` uses one private attribute for that (pinned `mcp<3`, covered by a test).

## [Stage 13 — MCP Servers] — 2026-10
### Added
- `mcp_servers/`: four MCP servers, one per data source, each bound to one incident (the id is not a tool argument): `logs` (get_incident_meta, log_overview, search_logs, log_window), `metrics` (get_metric_summary, list_metrics, query_metric), `deploys` (get_recent_deploys), `sandbox` (list_diagnostics, run_diagnostic). Failures are MCP tool errors tagged `[bad_args]` or `[unavailable]`.
- `backend.py`: tool logic; the five original tools are delegated to `IncidentToolbox`, so MCP output is byte-identical. New: `log_window`, `list_metrics`, `query_metric` (offsetless timestamps are read as UTC).
- `diagnostics.py`: four allow-listed read-only scripts (`error_timeline`, `first_seen`, `metric_vs_errors`, `service_breakdown`); each run is a separate isolated Python process (no shell, no code, validated arguments, minimal environment without API keys, timeout kill, output cap, CPU/memory rlimits on POSIX).
- `client.McpToolbox`: `IncidentToolbox` interface over MCP (in-memory or stdio transport), synchronous via a background event loop; originals keep the unchanged `TOOL_SPECS`, `extended=True` adds the MCP-only tools.
- `LLMReActAgent(toolbox_factory=...)` and harness `--toolbox direct|mcp` (llm_react only); the toolbox is closed after the run.
- Tests: `test_mcp_servers.py`, `test_mcp_integration.py` (28-incident parity, identical LLM requests through MCP, stdio round trip, timeouts, error mapping, mutation-checked).
- `mcp` optional extra (`mcp>=2.2,<3`).
### Notes
Unknown extra arguments are ignored by the MCP layer (IncidentToolbox rejects them); a missing required argument is rejected by the MCP layer with different wording. Both are still `bad_args`.

## [Stage 12 — Episodic Memory (rewrite)] — 2026-10
### Added
- `memory/episodic.py`: `Episode` (observation, resolved class, first diagnosed class, deterministic `Reflection`), `EpisodicMemory` (JSONL persistence in an explicit directory, atomic writes, hybrid recall over observations, `predict` with a dense-cosine similarity for gating, `lessons_for`, `forget`), injectable clock.
- `data/make_recurrences.py` + `data/recurrence/` + `eval/recurrence_gold.json`: 14 recurrences (each dev failure again, new seed, different service). Pinned v1/v2 data untouched.
- `eval.memory_eval`: replay history (predict, then write the resolved episode), classify queries by kb / memory / combined, report coverage, precision, rescued, misled, first-encounter-wrong subset, novel-class control, paired bootstrap; `--queries test` is the logged false-recall control.
- Tests: `test_episodic_memory.py`, `test_memory_eval.py` (mutation-checked; 33 mutants killed, 1 equivalent guard removed).
### Notes
The main dataset has no same-class recurrence inside a split (INC-002 and INC-020 are split across dev/test) and all incidents share one window start, so "better on the second encounter" could not be measured without the recurrence set.

## [Stage 11 — Reranking (rewrite)] — 2026-10
### Added
- `knowledge/rerank.py`: `Reranker` protocol, `OverlapReranker` (IDF-weighted query coverage + bigram coverage; lexical baseline), `CrossEncoderReranker` (opt-in `RERANKER=ce:<model>`, default `cross-encoder/ms-marco-MiniLM-L-6-v2`, `ml` extra), `CachedReranker` (`RERANK_CACHE_DIR`), `RerankingSearcher` (`rerank` mode: hybrid top-N, rescore, optional `alpha` blend with the min-max normalised fused score; ties keep hybrid order).
- `eval.retrieval_eval`: `--reranker`, `--candidates`, `--alpha`; adds a `rerank` mode and paired `rerank_vs_hybrid` deltas (MRR, hit@1, nDCG@10).
- `tests/test_rerank.py`; mutation-checked (22 mutants killed; one equivalent guard removed).
### Notes
Legacy reranker was an untrained random-weight MLP (seed 42) and its "BM25" feature ignored the query; superseded, left in place.

## [Stage 10 — Hybrid Retrieval (rewrite)] — 2026-10
### Added
- `knowledge/` package: tokenizer/stemmer (identifiers and HTTP codes kept), Okapi BM25 with real term frequency and Lucene idf, `HashingEmbedder` (deterministic lexical baseline, sha256-based), optional `SentenceTransformerEmbedder` (`EMBEDDER=st:<model>`, `ml` extra), disk-cached embeddings, `NumpyVectorStore` and `QdrantVectorStore` (local, path or URL) with identical results, standard weighted RRF (absent rankers contribute nothing, ties by id), `HybridSearcher` with `bm25|dense|hybrid` modes, KB loader with strict front matter, log-query builder that never reads gold.
- `data/kb/` (66 documents), `eval/retrieval_gold.json` (3 query styles per incident), `eval.retrieval_eval` (hit@k, MRR, nDCG@10, paired bootstrap deltas, test opening logged), `eval.validate_kb` (coverage and leak checks).
- OpenSpec change 017; `docs/AUDIT-STAGES-10-12.md` (Stage 10 section).
### Changed
- `qdrant-client` added to the `dev` extras (tests use it; skipped when absent).
### Notes
Legacy retrieval code is left untouched but superseded: its BM25 ignored term frequency, its "dense" retriever was TF-IDF, it had no Qdrant and RRF counted absent documents.

## [Stage 9 — LLM Tree-of-Thought + Plan-and-Solve] — 2026-10
### Added
- `agents/tree_of_thought.py`: hypothesize (verbatim-quoted observations, distinct alternatives each with a plan; current diagnosis is always H0), parallel branches that execute their plan with read-only tools and file a structured report, deterministic `score_branch` (only grounded items count), margin-gated switch (`SWITCH_MARGIN` 0.15, ties keep H0), solve step under the shared `accept_revision` policy, confidence cap on thin margins, optional confidence gate, failure isolation (`LLMConfigError` always propagates); `ToTOrchestrator`; `HierarchicalSwarm(tot=True)` (order ToT, debate, commander).
- Harness adapters `llm_tot`, `llm_swarm_full`; summary `tot_switched`, `tot_rejected`; per-incident `tot` trace; CLI `--agent tot|swarm-full`; OpenSpec change 016.
### Changed
- `accept_revision` is now a shared module-level function used by both debate and ToT.
### Notes
- 28-mutant check on `tree_of_thought.py`: all killed. Legacy `tree_of_thought_agent.py` kept for back-compat, not used by the LLM line.

## [Stage 8 — LLM debate and dataset v2] — 2026-10
### Added
- `agents/llm_debate.py`: defender vs forensic examiner (read-only tools) with evidence-gated challenges, rebuttal, arbiter and a deterministic, asymmetric acceptance policy; `DebatedOrchestrator`; `HierarchicalSwarm(debate=True)`.
- Harness adapters `llm_debate`, `llm_swarm_debate`; summary metrics `false_positive_rate`, `false_negative_rate`, `gold_false_alarms`, `debate_revised`, `debate_rejected`; per-incident `debate` trace.
- `eval.compare`: FP/FN per run and per-incident flips (fixed / newly broken).
- Dataset v2: INC-021..028 (4 hard false alarms, 4 false-negative traps); generator gained recovering excursions, sentinel gaps, fixed log events and WARN windows without changing INC-001..020 (hashes pinned).
- CLI `--agent debate|swarm-debate`; OpenSpec change 015.
### Changed
- Splits: dev 14 / test 14 (added before any measurement). Validator allows documented benign WARN lines in false alarms (`benign_warnings`).
### Notes
- Legacy `debate_mechanism.py` computed "false-positive rate" from confidence only; kept for back-compat, not used by the LLM line.

## [Pre-Stage-8 hardening] — 2026-10
### Added
- LLM layer (`llm/`): OpenAI-compatible client (Groq/Cerebras), cache, fallback, usage tracking, schema flattening, structured output with repair loop; `LLMUnavailable` (transient) vs `LLMConfigError` (permanent).
- `LLMReActAgent`, shared `ToolLoop` (duplicate/budget/bad-argument guards, `<tool_output>` wrapping, replan hook), `grounding.py`, validated `IncidentToolbox`.
- Stage 6: `LLMOrchestrator` (triage cannot drop workers, scoped workers, parallel, per-worker failure isolation).
- Stage 7: `LLMIncidentCommander` and `HierarchicalSwarm` (deterministic escalation policy, evidence-gated revision).
- `risk_guard.py`: deterministic risk-tier floor from `openspec/risk-classification.md`; only ever raises the tier (`--risk-guard`).
- Eval tooling: harness (dev/test split, tags, traces, resume), `eval.compare` (paired bootstrap), `eval.judge` (cross-model judge), `eval.preflight`.
- CLI: `diagnose --agent llm|orchestrator|swarm|legacy [--risk-guard]`.
- Docs: `docs/EVAL.md`, `docs/RESULTS.md`, OpenSpec changes 012-014.
### Changed
- CI: pinned `trivy-action` to v0.36.0; sandbox image drops setuptools/wheel; filesystem scan for `docker/`.
- README/ROADMAP rewritten to state real status (no stage is called measured without numbers).
### Fixed
- Tests no longer require `httpx` (openai 3.x uses `httpx2`).

## [Stage 11] — Reranking — 2026-09-28
### Added
- `src/incident_agent/schemas/reranking.py` — `RelevanceDelta` and `RerankedResult` Pydantic models:
  - `RelevanceDelta` with `incident_id`, `rrf_score`, `cross_encoder_score`, `delta`, `rank_change`
  - `RerankedResult` extends `AgentOutput` with `query`, `pre_rerank_rankings`, `post_rerank_rankings`, `relevance_deltas`, `mean_reciprocal_rank_before/after`, `precision_at_k_before/after`, `improvement_score` (computed), `reranking_time_ms`, `total_results`, `retrieval_time_ms`
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
  - `improvement_score` as `@computed_field` derived from before/after metrics
- `src/incident_agent/retrieval/reranker.py` — `CrossEncoderReranker` class:
  - `build_index(documents)` — builds TF-IDF vocabulary and BM25 baseline index
  - `rerank(query, candidate_ids, documents, top_k)` — cross-encoder re-scores query-document pairs jointly using interaction features and a deterministic MLP
  - `search(query, candidate_ids, documents, top_k)` — convenience method combining candidate selection and re-ranking
- `src/incident_agent/agents/reranker_agent.py` — `RerankerAgent` class:
  - `build_index(documents, metadata)` — builds full reranker index
  - `search(query, incident_ids)` — full pipeline: hybrid retrieval → cross-encoder reranking → before/after metrics
  - Produces schema-validated `RerankedResult`
  - Computes MRR and Precision@K before and after reranking
  - `improvement_score` measures net retrieval quality gain
- `tests/test_stage11.py` — 50 tests covering RelevanceDelta schema, RerankedResult schema, CrossEncoderReranker, RerankerAgent, MRR/Precision@K metrics, cross-cutting rule, and backward compatibility
- `src/incident_agent/schemas/__init__.py` — Added `RelevanceDelta`, `RerankedResult` exports
- `src/incident_agent/retrieval/__init__.py` — Added `CrossEncoderReranker` export
- `src/incident_agent/agents/__init__.py` — Added `RerankerAgent` export
- `src/incident_agent/__init__.py` — Added `RelevanceDelta`, `RerankedResult`, `RerankerAgent` exports
- `openspec/changes/011-reranking/proposal.md` — OpenSpec proposal

### Changed
- `src/incident_agent/schemas/__init__.py` — Added `RelevanceDelta` and `RerankedResult` exports
- `src/incident_agent/retrieval/__init__.py` — Added `CrossEncoderReranker` export
- `src/incident_agent/agents/__init__.py` — Added `RerankerAgent` export
- `src/incident_agent/__init__.py` — Added `RelevanceDelta`, `RerankedResult`, `RerankerAgent` exports

### Verified
- All 628 tests pass (50 Stage 11 + 578 existing)
- `CrossEncoderReranker.rerank()` returns results sorted by descending cross-encoder score
- `RerankerAgent.search()` produces a schema-valid `RerankedResult`
- Cross-encoder scores query-document pairs jointly (not independently like RRF)
- MRR and Precision@K computed correctly before and after reranking
- `improvement_score` computed correctly as `@computed_field`
- `RerankedResult.to_json()` → `from_json()` roundtrip works correctly
- `RerankedResult.format_report()` produces human-readable before/after comparison
- Cross-cutting rule verified: `RerankedResult` and `RelevanceDelta` are validated Pydantic objects
- Backward compatible: `ReActAgent`, `OrchestratorAgent`, `IncidentCommander`, `DebateMechanism`, `TreeOfThoughtAgent`, `RootCauseAgent`, `ForensicExaminerAgent`, `RetrievalAgent` still work unchanged
- `ruff check`, `ruff format`, `mypy`, `bandit` — all green

## [Stage 10] — 2026-09-27
### Added
- `src/incident_agent/schemas/vector_search.py` — `SearchHit` and `HybridSearchResult` Pydantic models:
  - `SearchHit` with `incident_id`, `score`, `ranker_source`, `rank`, `metadata`
  - `HybridSearchResult` extends `AgentOutput` with `query`, `bm25_results`, `dense_results`, `rrf_scores`, `fused_rankings`, `total_results`, `retrieval_time_ms`
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
- `src/incident_agent/retrieval/bm25.py` — `BM25Retriever` class:
  - `build_index(documents)` — builds inverted index from incident text
  - `search(query, top_k)` — BM25+ keyword retrieval (k1=1.5, b=0.75)
- `src/incident_agent/retrieval/dense.py` — `DenseRetriever` class:
  - `build_index(documents)` — builds numpy TF-IDF matrix
  - `search(query, top_k)` — cosine-similarity retrieval
- `src/incident_agent/retrieval/hybrid.py` — `HybridRetriever` and `rrf_rank()`:
  - `rrf_rank(bm25_results, dense_results, k=60)` — Reciprocal Rank Fusion
  - `HybridRetriever` orchestrates BM25 + Dense → RRF → fused results
- `src/incident_agent/agents/retrieval_agent.py` — `RetrievalAgent` class:
  - `build_index(documents, metadata)` — builds full hybrid index
  - `search(query, incident_ids)` — full pipeline producing `HybridSearchResult`
- `tests/test_stage10.py` — 58 tests covering SearchHit/HybridSearchResult schema, BM25 retrieval, dense retrieval, RRF fusion, hybrid search, RetrievalAgent, cross-cutting rule, and backward compatibility
- `src/incident_agent/retrieval/__init__.py` — retrieval package exports
- `openspec/changes/010-vector-db-hybrid-retrieval/proposal.md` — OpenSpec proposal
- `pyproject.toml` and `requirements.txt` — added `qdrant-client>=1.9` dependency
- `src/incident_agent/schemas/__init__.py` — Added `SearchHit`, `HybridSearchResult` exports
- `src/incident_agent/agents/__init__.py` — Added `RetrievalAgent` export
- `src/incident_agent/__init__.py` — Added `SearchHit`, `HybridSearchResult`, `RetrievalAgent` exports
- `README.md` — updated Stage 10 status and project layout
- `CHANGELOG.md` — this entry

### Changed
- `src/incident_agent/schemas/__init__.py` — Added `SearchHit` and `HybridSearchResult` exports
- `src/incident_agent/agents/__init__.py` — Added `RetrievalAgent` export
- `src/incident_agent/__init__.py` — Added `SearchHit`, `HybridSearchResult`, `RetrievalAgent` exports
- `pyproject.toml` — Added `qdrant-client>=1.9` to dependencies
- `requirements.txt` — Added `qdrant-client>=1.9`
- `README.md` — Updated Stage 10 status and test count

### Verified
- All ~570 tests pass (58 Stage 10 + 519 existing)
- `BM25Retriever.search()` returns relevant BM25+ keyword matches
- `DenseRetriever.search()` returns semantically similar results via TF-IDF cosine similarity
- `rrf_rank()` correctly fuses BM25 and dense rankings
- `HybridRetriever.search()` produces unified RRF-fused results
- `RetrievalAgent.search()` produces a schema-valid `HybridSearchResult`
- `HybridSearchResult` is a validated Pydantic object (Stage 4 cross-cutting rule)
- Cross-cutting rule verified: `SearchHit` and `HybridSearchResult` are validated Pydantic objects
- Backward compatible: `ReActAgent`, `OrchestratorAgent`, `IncidentCommander`, `DebateMechanism`, `TreeOfThoughtAgent`, `RootCauseAgent`, `ForensicExaminerAgent` still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 1] — 2026-09-14
### Added
- 20 synthetic incident scenarios (`data/incidents/INC-001`…`INC-020`) covering 19 categories
- Each scenario has `meta.json`, `logs.log`, `metrics.json`, `deploys.json`
- 20 gold answers (`eval/gold/INC-001.json`…`INC-020.json`) with root cause, evidence, remediation, and risk tier
- `eval/rubric.md` — composite scoring rubric with two hard gates (false-positive handling, safety/risk-tier compliance)
- `eval/gold_set.md` — mechanically generated human-readable summary table
- `data/generate_dataset.py` — deterministic, stdlib-only dataset generator
- `eval/validate_dataset.py` — mechanical integrity/leakage validator
- `eval/generate_gold_set_md.py` — regenerates `gold_set.md` from gold JSON files
- Full test suite (`tests/`) with 187 tests covering generator, validator, integrity, leakage, false-alarm, recurrence, and distractor invariants
- `requirements.txt` — project dependencies (torch, numpy)
- `tests/test_torch_env.py` — verifies torch/numpy environment
- `tests/test_dataset.py` — comprehensive dataset tests
- `.venv/` Python environment with PyTorch 2.14.0+cpu and NumPy 2.4.6
### Changed
- `.gitignore` — fixed merge conflict markers, added eval/data artifact paths
### Verified
- All 187 tests pass (`python -m pytest tests/ -v`)
- `data/generate_dataset.py` runs cleanly and is deterministic across runs
- `eval/validate_dataset.py` passes with no problems found
- PyTorch 2.14.0+cpu and NumPy 2.4.6 available and interoperable
- Gold answers kept structurally separate from agent-visible data (no eval leakage)

## [Stage 9] — 2026-09-25
### Added
- `src/incident_agent/schemas/tree_of_thought.py` — `Hypothesis` and `TreeOfThoughtResult` Pydantic models:
  - `Hypothesis` model with `hypothesis_id`, `scenario`, `root_cause`, `confidence`, `supporting_evidence`, `reasoning_steps`, `category`, `risk_tier`
  - `TreeOfThoughtResult` extends `AgentOutput` with `hypotheses`, `selected_hypothesis`, `selected_hypothesis_index`, `reasoning_steps`, `selected_evidence`, `plan_and_solve_validation`, `accuracy_improvement`, `final_diagnosis`, `hypothesis_scores`
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
  - Pydantic validators for `accuracy_improvement`, `confidence`, and `verdict`
- `src/incident_agent/agents/tree_of_thought_agent.py` — `TreeOfThoughtAgent` class:
  - `generate_hypotheses(diagnosis)` — produces N parallel alternative root-cause hypotheses
  - `evaluate_hypotheses(hypotheses)` — scores each hypothesis against evidence
  - `select_best_hypothesis(hypotheses, scores)` — picks the highest-scoring hypothesis
  - `plan_and_solve(hypothesis, diagnosis)` — validates selected hypothesis via Plan-and-Solve
  - `run(diagnosis)` — full Tree-of-Thought pipeline producing `TreeOfThoughtResult`
- `tests/test_stage9.py` — 58 tests covering Hypothesis schema, TreeOfThoughtResult schema, hypothesis generation and scoring, best-hypothesis selection, Plan-and-Solve validation, accuracy improvement, schema validation, and backward compatibility
- `src/incident_agent/schemas/__init__.py` — Added `Hypothesis`, `TreeOfThoughtResult` exports
- `src/incident_agent/agents/__init__.py` — Added `TreeOfThoughtAgent` export
- `src/incident_agent/__init__.py` — Added `Hypothesis`, `TreeOfThoughtResult`, `TreeOfThoughtAgent` exports

### Changed
- `src/incident_agent/schemas/__init__.py` — Added `Hypothesis` and `TreeOfThoughtResult` exports
- `src/incident_agent/agents/__init__.py` — Added `TreeOfThoughtAgent` export
- `src/incident_agent/__init__.py` — Added `Hypothesis`, `TreeOfThoughtResult`, `TreeOfThoughtAgent` exports

### Verified
- All 519 tests pass (58 Stage 9 + 461 existing)
- `TreeOfThoughtAgent.run()` produces a schema-valid `TreeOfThoughtResult`
- Multiple hypotheses (≥2) are generated in parallel
- Best hypothesis is selected based on highest evaluation score
- Plan-and-Solve validation is performed on the selected hypothesis
- Accuracy improvement is measurable (0.0–1.0 range)
- Cross-cutting rule verified: `TreeOfThoughtResult` is a validated Pydantic object
- Backward compatible: `ReActAgent`, `OrchestratorAgent`, `IncidentCommander`, and `DebateMechanism` still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 8] — 2026-09-23
### Added
- `src/incident_agent/schemas/debate.py` — `Argument` and `DebateOutcome` Pydantic models:
  - `Argument` model with `agent`, `point`, `evidence_refs`, `confidence`
  - `DebateOutcome` extends `AgentOutput` with `original_diagnosis`, `root_cause_arguments`, `forensic_challenges`, `verdict`, `false_positive_rate_before/after`, `confidence_adjustment`, `final_diagnosis`, `debate_rounds`, `reasoning_steps`, `recommendation`
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
  - Pydantic validators for `verdict` and confidence bounds
- `src/incident_agent/agents/root_cause_agent.py` — `RootCauseAgent` class:
  - `build_arguments(diagnosis)` — produces supporting arguments citing evidence
  - `analyze(diagnosis)` — entry point returning list of `Argument` objects
- `src/incident_agent/agents/forensic_examiner_agent.py` — `ForensicExaminerAgent` class:
  - `build_challenges(diagnosis)` — produces challenges looking for false positives
  - `challenge(diagnosis)` — entry point returning list of `Argument` objects
- `src/incident_agent/agents/debate_mechanism.py` — `DebateMechanism` class:
  - `run(diagnosis)` — full debate pipeline producing `DebateOutcome`
  - `calculate_false_positive_rate()` — deterministic FPR calculation
  - `determine_verdict()` — verdict logic (confirmed/challenged/revised)
- `openspec/changes/008-debate-mechanism/proposal.md` — OpenSpec proposal
- `tests/test_stage8.py` — 55 tests covering Argument schema, DebateOutcome schema, RootCauseAgent, ForensicExaminerAgent, DebateMechanism pipeline, false-positive rate comparison, verdict determination, debate rounds, schema validation, and backward compatibility
- `src/incident_agent/schemas/__init__.py` — Added `Argument`, `DebateOutcome` exports
- `src/incident_agent/agents/__init__.py` — Added `RootCauseAgent`, `ForensicExaminerAgent`, `DebateMechanism` exports
- `src/incident_agent/__init__.py` — Added `Argument`, `DebateOutcome`, all new agents

### Verified
- All 461 tests pass (55 Stage 8 + 406 existing)
- `DebateMechanism.run()` produces a schema-valid `DebateOutcome`
- False-positive rate comparison shows before/after FPR values
- Verdict is "confirmed" when root-cause arguments outweigh challenges
- Verdict is "challenged" when challenges are moderate
- Verdict is "revised" when challenges outweigh root-cause arguments
- Cross-cutting rule verified: `DebateOutcome` is a validated Pydantic object
- Backward compatible: `ReActAgent`, `OrchestratorAgent`, and `IncidentCommander` still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 7] — 2026-09-22
### Added
- `src/incident_agent/agents/incident_commander.py` — Incident Commander agent:
  - `IncidentCommander` class sits above OrchestratorAgent in the hierarchy
  - `synthesize_narrative()` — builds unified incident narrative from diagnosis and worker findings
  - `determine_escalation()` — makes escalation decision (escalate / monitor / resolve)
  - `assess_severity()` — evaluates overall severity from risk tier and evidence
  - `build_reasoning()` — builds commander-level reasoning steps
  - `build_evidence()` — aggregates evidence from diagnosis and all workers
  - `build_recommendation()` — produces commander-level recommendation
  - `run()` — full commander pipeline producing CommanderDiagnosis
- `src/incident_agent/schemas/command_diagnosis.py` — CommanderDiagnosis Pydantic model:
  - Extends AgentOutput with narrative, escalation_decision, severity_assessment
  - Contains the full underlying Diagnosis and all WorkerFinding objects
  - All fields validated by Pydantic (cross-cutting rule)
  - `format_report()`, `to_json()`, `from_json()`, `model_dump()` methods
- `src/incident_agent/schemas/__init__.py` — Exports CommanderDiagnosis
- `src/incident_agent/agents/__init__.py` — Exports IncidentCommander
- `src/incident_agent/__init__.py` — Exports IncidentCommander and CommanderDiagnosis
- `openspec/changes/007-hierarchical-swarm/proposal.md` — OpenSpec proposal
- `tests/test_stage7.py` — 45+ tests covering CommanderDiagnosis schema, escalation logic, severity assessment, narrative synthesis, reasoning, evidence, recommendation, end-to-end pipeline, and backward compatibility

### Changed
- `src/incident_agent/schemas/__init__.py` — Added CommanderDiagnosis export
- `src/incident_agent/agents/__init__.py` — Added IncidentCommander export
- `src/incident_agent/__init__.py` — Added IncidentCommander and CommanderDiagnosis exports

### Verified
- All 401+ tests pass (45+ Stage 7 + 356 existing)
- IncidentCommander.run() produces a schema-valid CommanderDiagnosis
- CommanderDiagnosis contains the full underlying Diagnosis object
- CommanderDiagnosis contains all WorkerFinding objects
- Escalation decisions are deterministic based on risk tier and confidence
- Severity assessment reflects risk tier and evidence count
- Narrative synthesis combines diagnosis root cause with worker summaries
- CommanderDiagnosis.to_json() → from_json() roundtrip works correctly
- Cross-cutting rule verified: CommanderDiagnosis is a validated Pydantic object
- Backward compatible: ReActAgent and OrchestratorAgent still work unchanged
- ruff check, ruff format, mypy, bandit — all green

## [Stage 6] — 2026-09-21
### Added
- `src/incident_agent/agents/worker.py` — Specialist worker agents:
  - `WorkerAgent` base class (ABC) with `observe()` / `analyze()` / `run()` pipeline
  - `LogWorker` — parses logs, extracts error patterns, produces `WorkerFinding`
  - `MetricsWorker` — analyzes metric time series, detects anomalies, produces `WorkerFinding`
  - `DeployHistoryWorker` — correlates deploys with incident window, produces `WorkerFinding`
- `src/incident_agent/agents/orchestrator.py` — OrchestratorAgent (triage agent):
  - `OrchestrationState` — lightweight state machine (`INITIALIZED` → `DISPATCHED` → `PROCESSING` → `COMPLETED` → `SYNTHESIZED`)
  - `OrchestrationStatus` — tracks completed/failed workers and findings
  - `OrchestratorAgent.classify_incident()` — reads metadata and classifies risk
  - `OrchestratorAgent.dispatch_workers()` — creates and runs all three specialist workers
  - `OrchestratorAgent.synthesize()` — combines worker findings into a unified `Diagnosis`
  - `OrchestratorAgent.run()` — full orchestration pipeline
- `src/incident_agent/schemas/agent_output.py` — New `WorkerFinding` Pydantic model:
  - `worker_type`, `incident_id`, `evidence`, `reasoning_steps`, `confidence`, `summary`
  - All fields validated by Pydantic (cross-cutting rule)
- `src/incident_agent/schemas/__init__.py` — Exports `WorkerFinding` and `EvidenceType`
- `src/incident_agent/agents/__init__.py` — Exports all worker and orchestrator classes
- `src/incident_agent/__init__.py` — Exports all Stage 6 additions
- `openspec/changes/006-orchestrator-workers/proposal.md` — OpenSpec proposal
- `tests/test_stage6.py` — 52 tests covering orchestration state machine, all three workers, orchestrator dispatch/synthesize/run, backward compatibility, and schema validation

### Changed
- `src/incident_agent/agents/react_agent.py` — `run()` still uses plan-based execution with replanning (Stage 5); now available alongside `OrchestratorAgent`
- `src/incident_agent/__init__.py` — Added `OrchestratorAgent`, `OrchestrationState`, `LogWorker`, `MetricsWorker`, `DeployHistoryWorker`, `WorkerAgent`, `WorkerFinding`, `EvidenceType`

### Verified
- All 356 tests pass (52 Stage 6 + 304 existing)
- Orchestrator dispatches all three workers and synthesizes a valid `Diagnosis`
- Each worker produces a schema-valid `WorkerFinding`
- `OrchestrationState` tracks all state transitions correctly
- Workers handle failures gracefully without crashing the orchestration
- Backward compatible: `ReActAgent` still produces `single_agent_react` diagnoses
- Cross-cutting rule verified: every agent output is a validated Pydantic object
- ruff check, ruff format, mypy, bandit — all green

## [Stage 5] — 2026-09-19
### Added
- `src/incident_agent/workflows/plan.py` — Plan, PlanStep, PlanStepType, StepFailure, generate_plan()
  - `Plan` dataclass with ordered `PlanStep` sequence, replan_count tracking
  - `PlanStep` frozen dataclass with step_type, description, order
  - `StepFailure` exception carrying the failed step and reason
  - `generate_plan(incident_id, strategy)` supporting "standard", "log_only", "metrics_only"
- `src/incident_agent/agents/react_agent.py` — Dynamic planning and error recovery (Stage 5)
  - `ReActAgent.generate_plan()` — produces a diagnostic plan before execution
  - `ReActAgent.execute_plan()` — runs all plan steps, raising StepFailure on invalid results
  - `ReActAgent.replan()` — rebuilds the plan from failure feedback (skip failed step, substitute alternative strategy)
  - `ReActAgent.run()` — updated to execute plan with replanning loop (max_replans=2 default)
  - Cross-cutting rule maintained: every output is still a schema-validated Pydantic Diagnosis
- `src/incident_agent/agents/__init__.py` — exports Plan, StepFailure
- `src/incident_agent/__init__.py` — exports Plan, StepFailure
- `src/incident_agent/workflows/__init__.py` — exports Plan, PlanStep, StepFailure, generate_plan
- `tests/test_stage5.py` — 35 tests covering plan generation, step execution, replanning, error recovery, schema validation

### Changed
- `src/incident_agent/agents/react_agent.py` — `run()` now uses plan-based execution with replanning instead of a single observe/reason/act pass

### Verified
- All 304 tests pass (269 existing + 35 Stage 5)
- Replanning recovers from injected StepFailure and produces schema-valid Diagnosis objects
- Plan generation works for all three strategies (standard, log_only, metrics_only)
- Backward compatible: all Stage 3 and Stage 4 tests still pass unchanged

## [Stage 4] — 2026-09-18
### Added
- `src/incident_agent/schemas/` package with Pydantic models:
  - `Diagnosis` — replaces `dataclasses.Diagnosis` with full schema validation
  - `EvidenceItem`, `ReasoningStep`, `IncidentMetadata`, `MetricAnomaly`, `AgentOutput`, `RiskTier`
- `src/incident_agent/prompts/` package with prompt library:
  - `system_prompt.py` — system prompt template with role, constraints, and output schema
  - `few_shot.py` — three curated few-shot examples covering diverse categories and risk tiers
  - `builder.py` — `build_prompt()` utility to assemble complete prompts
- `ReActAgent` updated to produce Pydantic-validated `Diagnosis` objects
- Cross-cutting rule enforced: every agent output is a validated Pydantic object
- `tests/test_stage4.py` — 49 schema validation and prompt library tests
- OpenSpec proposal `openspec/changes/004-structured-output-prompt-engineering/proposal.md`
- Pydantic added to `pyproject.toml` and `requirements.txt`
### Changed
- `src/incident_agent/agents/react_agent.py` — now produces Pydantic `Diagnosis` instead of `dataclasses.Diagnosis`
- `src/incident_agent/__init__.py` — exports all Pydantic schema types
- `src/incident_agent/agents/__init__.py` — exports Pydantic `Diagnosis`
- `src/incident_agent/schemas/__init__.py` — new canonical schema exports
### Verified
- All 82 tests pass (49 Stage 4 + 28 Stage 3 + 5 smoke)
- Schema validation catches invalid inputs (missing fields, out-of-bounds confidence, empty evidence)
- JSON round-trip (`to_json` → `from_json`) produces valid Diagnosis objects
- Prompt builder produces valid structured prompts with few-shot examples
- Backward compatible: existing `format_report()` and CLI work unchanged

## [Stage 3] — 2026-09-16
### Added
- `src/incident_agent/agents/react_agent.py` — ReActAgent with observe/reason/act/repeat loop
- `src/incident_agent/tools/` — tool functions: read_meta, read_logs, find_error_logs, detect_anomaly_timestamps, get_recent_deploys, find_metric_spikes, read_metrics, read_deploys
- `src/incident_agent/cli.py` — diagnose subcommand
- `Diagnosis` dataclass with structured output and format_report()
- 28 comprehensive Stage 3 tests
### Changed
- README.md — updated Stage 3 status
### Verified
- All 220 tests passing
- ruff, mypy, bandit clean

## [Stage 2] — 2026-09-15
### Added
- Project skeleton: `src/incident_agent/` package with subpackages (`agents/`, `tools/`, `memory/`, `workflows/`, `eval/`)
- `pyproject.toml` with hatchling build, ruff/bandit/mypy config
- CI pipeline (`.github/workflows/ci.yml`): ruff, bandit, mypy, pytest, trivy, dataset regeneration check
- `Makefile` with 10 targets (`make ci`, `make sandbox-build`, `make sandbox-shell`, etc.)
- `.pre-commit-config.yaml` (ruff, pre-commit hooks)
- Docker configuration: `Dockerfile`, `docker-compose.yml`, `docker/sandbox/Dockerfile` (hardened non-root image)
- `docker/sandbox/README.md` — sandbox invocation patterns and rationale
- `src/incident_agent/cli.py` — console script entry point placeholder
- `src/incident_agent/py.typed` — marker file for type-aware imports
- 5 Stage-2 smoke tests (`tests/test_smoke.py`) — package install, CLI, subpackage imports
- **192 tests total, all passing**
### Changed
- `.gitignore` — added data/generated/, eval/results/ paths
- README.md — updated Stage 2 status, corrected test count to 192
### Verified
- All 192 tests pass
- ruff check, ruff format, mypy, bandit — all green
- Docker image builds and runs as non-root user
- Sandbox has no network access by default

## [Stage 0] — 2026-09-13
### Added
- `openspec/proposal.md` — Scope, architecture overview, design decisions, acceptance criteria
- `openspec/agent-responsibility-matrix.md` — Per-agent responsibility, read/write scope, risk ceiling
- `openspec/risk-classification.md` — Low / medium / high risk taxonomy with governance rules
- `openspec/comparison-bmad-speckit-openspec.md` — Methodology comparison and rationale for choosing OpenSpec
- `openspec/README.md` — OpenSpec workflow documentation
- `CHANGELOG.md` — This file

---

## Template for future stages

Each stage adds its own section here following this pattern:

```markdown
## [Stage N] — <Title> — <YYYY-MM-DD>
### Added
- What was delivered
### Changed
- What was modified from prior stages
### Verified
- Acceptance criteria met
```
