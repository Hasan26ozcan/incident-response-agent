# 017 - Hybrid retrieval (Stage 10)

## Why
The legacy retrieval layer was not what the roadmap says (see `docs/AUDIT-STAGES-10-12.md`). Stage 10 needs a real BM25 + dense + RRF layer, a vector database and a measurable quality bar before agents may use it.

## What
- `knowledge/`: tokenizer, Okapi BM25, embedders (hashing baseline, optional sentence-transformers, disk cache), vector stores (numpy, Qdrant), weighted RRF, `HybridSearcher` (modes bm25/dense/hybrid), KB loader, log-query builder.
- `data/kb/`: runbooks, post-mortems, reference notes, written without the gold answers.
- `eval/retrieval_gold.json`, `eval.retrieval_eval`, `eval.validate_kb`.

## Design decisions
- RRF: absent rankers contribute nothing; ties broken by id; deterministic everywhere.
- Hashing embedder is explicitly a lexical baseline; semantic claims require an `st:` run.
- Qdrant is optional (local mode in tests) and must match the numpy store exactly.
- Agents are not wired to retrieval yet: that changes prompts and invalidates caches, so it needs its own flagged change.

## Out of scope
Reranking (Stage 11), episodic memory (Stage 12), agent tool integration.

## Acceptance
Validator clean (coverage, no gold leakage); tests incl. mutation check; dev quality floor guarded by a regression test; results documented with confidence intervals.
