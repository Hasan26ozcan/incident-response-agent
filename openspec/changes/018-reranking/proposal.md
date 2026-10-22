# 018 - Reranking (Stage 11)

## Why
The legacy reranker was not a cross-encoder (see `docs/AUDIT-STAGES-10-12.md`). Stage 11 needs a real reranker and a before/after measurement on the Stage 10 retrieval gold.

## What
- `knowledge/rerank.py`: `Reranker` protocol; `OverlapReranker` (deterministic lexical baseline/test double); `CrossEncoderReranker` (opt-in, local, free); `CachedReranker`; `RerankingSearcher` (mode `rerank`, `candidates`, `alpha` blend).
- `eval.retrieval_eval --reranker/--candidates/--alpha`: `rerank` row and paired `rerank_vs_hybrid` deltas.

## Design decisions
- Reranker only reorders hybrid candidates; it cannot add documents. `candidates` is never below k.
- Pure rerank by default (alpha 0); blend uses min-max normalised scores of both sides; ties keep hybrid order.
- Lexical baseline is explicitly not a neural claim; the cross-encoder is opt-in because it needs a model download.
- Agents are not wired to retrieval/reranking yet.

## Acceptance
Tests incl. mutation check; before/after table with confidence intervals; reranking kept only if dev MRR/hit@1 improve with a CI clear of zero.
