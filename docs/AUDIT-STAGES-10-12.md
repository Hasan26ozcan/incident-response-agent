# Audit: Stages 10-12 (legacy code)

Status: Stage 10 audited and rewritten (this change). Stages 11 and 12 not yet audited; treat their legacy code as untrusted.

## Stage 10 findings (legacy `retrieval/`, `agents/retrieval_agent.py`)

| # | Finding | Evidence | Consequence |
|--:|---------|----------|-------------|
| 1 | BM25 term frequency was always 1 | a document with "cpu" five times ranked below one with a single mention | not BM25; length/tf saturation absent |
| 2 | The "dense" retriever was TF-IDF | no embedding model anywhere | no semantic retrieval at all |
| 3 | No vector database | no Qdrant code or dependency use | roadmap item unmet |
| 4 | RRF gave absent documents a phantom rank | fusion added a term for documents a ranker never returned | scores biased towards documents only one ranker saw |
| 5 | No retrieval ground truth | no relevance labels | quality unmeasured |

Resolution: new `knowledge/` package and retrieval eval (OpenSpec 017). Legacy files are left in place but superseded.

## Stage 11 findings (legacy `retrieval/reranker.py`, `agents/reranker_agent.py`)

| # | Finding | Consequence |
|--:|---------|-------------|
| 1 | The "cross-encoder" is an MLP with random fixed weights (`RandomState(42)`), never trained | scores are noise shaped by TF-IDF features; not a learned relevance model |
| 2 | Its "BM25" feature is computed over corpus tokens, independent of the query | same value for every query |
| 3 | Reranker agent re-instantiated the legacy retriever on every search and used its own tokenizer | slow, inconsistent with Stage 10 |
| 4 | No relevance labels, so "before/after" had nothing to be measured against | claim unmeasured |

Resolution: `knowledge/rerank.py` plus paired before/after evaluation (OpenSpec 018).

## Stage 12 findings (legacy `memory/store.py`, `agents/self_improvement_agent.py`, `schemas/memory.py`)

| # | Finding | Consequence |
|--:|---------|-------------|
| 1 | Correctness was `diagnosis.root_cause == gold_root_cause` (free-text string equality) | almost always "wrong"; not a measure of diagnosis quality |
| 2 | `accuracy_after` was set to 1.0 when the answer matched, else copied from `accuracy_before` | the "improvement" was manufactured, not measured |
| 3 | Gold root cause was passed into the agent and embedded in `updated_strategy` ("focus on <gold> patterns") | gold leakage into memory |
| 4 | Timestamp hard-coded to `2026-09-30`; `embedding_text` was just `"<id> <root cause>"`; no embedding or similarity search existed | "embedded" memory did not exist; retrieval was tag lookup |
| 5 | Episodes were written as JSON files inside `src/incident_agent/memory/store/` (tests left INC-013/014/015 files in the source tree) | state in the package directory; test pollution |
| 6 | The "second encounter" test used two hand-built diagnoses and asserted the number the code assigns | self-fulfilling test |
| 7 | Dataset cannot measure recurrence: no same-class pair inside a split, one shared window start | needed a recurrence set |

Resolution: `memory/episodic.py`, the recurrence set and `eval/memory_eval.py` (OpenSpec 019). Legacy files are left in place but superseded; delete `memory/store/*.json` artifacts from your tree when convenient.
