"""Retrieval evaluation (Stage 10/11): does the hybrid layer put the right runbook and post-mortem on top?

For each incident the query is either ``log`` (built from observable logs/metrics/deploys, see
``knowledge/query.py``), ``paraphrase`` (an engineer's symptom description from ``eval/retrieval_gold.json``)
or ``symptom`` (a vague description that deliberately avoids the runbooks' vocabulary).
A document is *relevant* (grade 2) when it is about the incident's primary failure class, *useful* (grade 1) when
about a listed secondary class, otherwise 0. Reported per query style and per mode (bm25 / dense / hybrid):

* hit@1/3/5: a grade-2 document appears in the top k
* mrr: reciprocal rank of the first grade-2 document
* ndcg@10: graded nDCG with gains 2^grade - 1

Usage: python -m incident_agent.eval.retrieval_eval --split dev --style both --embedder hashing --tag s10_hash
Tune only on the dev split; opening test is logged in eval/test_openings.log like the agent harness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_agent.eval.compare import paired_bootstrap
from incident_agent.eval.harness import RESULTS_DIR, ROOT, SPLITS_FILE, _log_test_opening, load_split
from incident_agent.knowledge.corpus import KB_DIR, KBDoc, load_corpus
from incident_agent.knowledge.embedders import Embedder, EmbedderError, build_embedder
from incident_agent.knowledge.hybrid import DEFAULT_RRF_K, MODES, HybridSearcher, SearchHit
from incident_agent.knowledge.query import INCIDENTS_DIR, QueryError, build_log_query
from incident_agent.knowledge.vector_store import VectorStoreError, build_store

GOLD_FILE = ROOT / "eval" / "retrieval_gold.json"
STYLES = ("log", "paraphrase", "symptom")
METRICS = ("hit@1", "hit@3", "hit@5", "mrr", "ndcg@10")


class GoldError(ValueError):
    pass


def load_gold(path: Path | None = None) -> dict[str, dict[str, Any]]:
    data = json.loads((path or GOLD_FILE).read_text())
    entries: dict[str, dict[str, Any]] = data["incidents"]
    for iid, e in entries.items():
        if not e.get("primary") or not e.get("paraphrase") or not e.get("symptom"):
            raise GoldError(f"{iid}: needs 'primary', 'paraphrase' and 'symptom'")
        if e["primary"] in e.get("secondary", []):
            raise GoldError(f"{iid}: primary class is also listed as secondary")
    return entries


def grade(doc: KBDoc, entry: Mapping[str, Any]) -> int:
    if doc.failure_class is None:
        return 0
    if doc.failure_class == entry["primary"]:
        return 2
    return 1 if doc.failure_class in entry.get("secondary", []) else 0


def _dcg(grades: Sequence[int]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def score_ranking(ranked_ids: Sequence[str], docs: Mapping[str, KBDoc], entry: Mapping[str, Any]) -> dict[str, float]:
    grades = [grade(docs[i], entry) for i in ranked_ids]
    first = next((i + 1 for i, g in enumerate(grades) if g == 2), 0)
    ideal = sorted((grade(d, entry) for d in docs.values()), reverse=True)[:10]
    ideal_dcg = _dcg(ideal)
    return {
        "hit@1": float(2 in grades[:1]),
        "hit@3": float(2 in grades[:3]),
        "hit@5": float(2 in grades[:5]),
        "mrr": 1.0 / first if first else 0.0,
        "ndcg@10": _dcg(grades[:10]) / ideal_dcg if ideal_dcg else 0.0,
        "first_relevant_rank": float(first),
    }


def query_for(incident_id: str, style: str, gold: Mapping[str, Mapping[str, Any]], incidents_dir: Path | None) -> str:
    if style == "log":
        return build_log_query(incident_id, incidents_dir)
    if style in ("paraphrase", "symptom"):
        return str(gold[incident_id][style])
    raise ValueError(f"style must be one of {STYLES}")


def evaluate(
    searcher: HybridSearcher,
    docs: Sequence[KBDoc],
    gold: Mapping[str, Mapping[str, Any]],
    ids: Sequence[str],
    styles: Sequence[str] = STYLES,
    modes: Sequence[str] = MODES,
    k: int = 10,
    incidents_dir: Path | None = None,
) -> dict[str, Any]:
    by_id = {d.id: d for d in docs}
    unknown = [i for i in ids if i not in gold]
    if unknown:
        raise GoldError(f"no retrieval gold for {unknown}")
    rows: list[dict[str, Any]] = []
    for style in styles:
        for iid in ids:
            query = query_for(iid, style, gold, incidents_dir)
            for mode in modes:
                hits: list[SearchHit] = searcher.search(query, k, mode)
                scores = score_ranking([h.doc_id for h in hits], by_id, gold[iid])
                rows.append(
                    {
                        "incident_id": iid,
                        "style": style,
                        "mode": mode,
                        "primary": gold[iid]["primary"],
                        **scores,
                        "top": [h.doc_id for h in hits[:5]],
                    }
                )
    summary: dict[str, dict[str, dict[str, float]]] = {}
    for style in styles:
        summary[style] = {}
        for mode in modes:
            sel = [r for r in rows if r["style"] == style and r["mode"] == mode]
            summary[style][mode] = {"n": len(sel), **{m: round(sum(r[m] for r in sel) / len(sel), 4) for m in METRICS}}
    return {"summary": summary, "incidents": rows}


def deltas(rows: Sequence[Mapping[str, Any]], style: str, a: str, b: str, metric: str = "mrr") -> dict[str, Any]:
    """Paired per-incident difference of ``a`` minus ``b`` with a bootstrap 95 % CI."""
    by = {(r["incident_id"], r["mode"]): r[metric] for r in rows if r["style"] == style}
    ids = sorted({i for i, _ in by})
    diffs = [by[(i, a)] - by[(i, b)] for i in ids if (i, a) in by and (i, b) in by]
    lo, hi = paired_bootstrap(diffs)
    return {
        "n": len(diffs),
        "mean": round(sum(diffs) / len(diffs), 4) if diffs else 0.0,
        "ci": (round(lo, 4), round(hi, 4)),
        "wins": sum(d > 1e-9 for d in diffs),
        "losses": sum(d < -1e-9 for d in diffs),
    }


def kb_fingerprint(kb_dir: Path | None = None, gold_file: Path | None = None) -> str:
    h = hashlib.sha256()
    for f in sorted((kb_dir or KB_DIR).glob("*.md")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    h.update((gold_file or GOLD_FILE).read_bytes())
    return h.hexdigest()[:16]


def parse_weights(raw: str | None) -> dict[str, float] | None:
    if not raw:
        return None
    out: dict[str, float] = {}
    for part in raw.split(","):
        name, sep, value = part.partition("=")
        if not sep or name.strip() not in ("bm25", "dense"):
            raise ValueError(f"bad weight {part!r}; use bm25=<number>,dense=<number>")
        out[name.strip()] = float(value)
    return out


def format_table(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for style, modes in report["summary"].items():
        lines.append(f"\nquery style: {style}")
        lines.append(f"{'mode':<8}" + "".join(f"{m:>9}" for m in METRICS) + f"{'n':>5}")
        for mode, s in modes.items():
            lines.append(f"{mode:<8}" + "".join(f"{s[m]:>9.3f}" for m in METRICS) + f"{s['n']:>5}")
    return "\n".join(lines)


def result_path(tag: str | None, results_dir: Path | None = None) -> Path:
    return (results_dir or RESULTS_DIR) / (f"retrieval__{tag}.json" if tag else "retrieval.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate BM25 / dense / hybrid retrieval against the retrieval gold")
    ap.add_argument("--split", default="dev", choices=["all", "dev", "test"])
    ap.add_argument("--ids", default=None, help="comma-separated incident ids (overrides --split)")
    ap.add_argument("--style", default="both", choices=[*STYLES, "both"])
    ap.add_argument(
        "--embedder", default=None, help="hashing | hashing:<dim> | st:<model>  (default: $EMBEDDER or hashing)"
    )
    ap.add_argument(
        "--store", default=None, choices=["numpy", "qdrant"], help="vector store (default: $VECTOR_STORE or numpy)"
    )
    ap.add_argument("--rrf-k", type=int, default=DEFAULT_RRF_K)
    ap.add_argument("--depth", type=int, default=20, help="candidates each retriever contributes to fusion")
    ap.add_argument("--weights", default=None, help="RRF weights, e.g. bm25=1,dense=2")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args(argv)

    try:
        gold = load_gold()
        docs = load_corpus()
        weights = parse_weights(args.weights)
        embedder: Embedder = build_embedder(args.embedder)
        searcher = HybridSearcher(
            embedder, build_store(args.store), rrf_k=args.rrf_k, depth=args.depth, weights=weights
        )
        searcher.index(docs)
    except (EmbedderError, VectorStoreError, GoldError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    elif args.split == "all":
        ids = sorted(gold)
    else:
        ids = load_split(args.split, SPLITS_FILE)
    if args.split == "test" and not args.ids:
        _log_test_opening("retrieval", args.tag, ids)
        print("NOTE: TEST SET OPENED (logged in eval/test_openings.log). Do not tune on these results.")
    styles = STYLES if args.style == "both" else (args.style,)
    try:
        report = evaluate(searcher, docs, gold, ids, styles, MODES, incidents_dir=INCIDENTS_DIR)
    except (GoldError, QueryError) as exc:
        print(f"ERROR: {exc}")
        return 2
    report["meta"] = {
        "tag": args.tag,
        "split": "ids" if args.ids else args.split,
        "embedder": embedder.name,
        "store": type(searcher.store).__name__,
        "rrf_k": args.rrf_k,
        "depth": args.depth,
        "weights": weights,
        "n_docs": len(docs),
        "kb_fingerprint": kb_fingerprint(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    report["deltas"] = {
        style: {
            "hybrid_vs_bm25_mrr": deltas(report["incidents"], style, "hybrid", "bm25"),
            "hybrid_vs_dense_mrr": deltas(report["incidents"], style, "hybrid", "dense"),
        }
        for style in styles
    }
    out = result_path(args.tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"embedder={embedder.name} store={type(searcher.store).__name__} docs={len(docs)} incidents={len(ids)}")
    print(format_table(report))
    for style, d in report["deltas"].items():
        for name, v in d.items():
            print(
                f"[{style}] {name}: mean {v['mean']:+.3f} CI {v['ci']} "
                f"wins {v['wins']} losses {v['losses']} (n={v['n']})"
            )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
