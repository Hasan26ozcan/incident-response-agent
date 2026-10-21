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

## Stage 11-12 (preliminary, to be confirmed in their own audits)

* Stage 12 legacy compared answers by gold string equality and leaked gold text into memory.
