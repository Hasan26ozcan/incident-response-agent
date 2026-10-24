"""Episodic-memory evaluation (Stage 12): does remembering past incidents help on the second encounter?

Protocol (no LLM calls, no API cost):

1. *Seeding.* The history incidents (dev split) are replayed in order. For each one the knowledge-base predictor
   guesses a failure class from the observable log query, memory is asked (it abstains at first), and only then is
   the resolved class from the retrieval gold written as an episode, with a deterministic reflection.
2. *Queries.* Each query incident is classified three ways from its observable log query alone:
   ``kb`` (top knowledge-base hit), ``memory`` (score-weighted vote of the recalled episodes) and ``combined``
   (memory when the best episode's dense cosine reaches ``--min-similarity``, otherwise the knowledge base).
3. Reported: accuracy of each, memory coverage and precision, incidents the memory *rescued* or *misled*, and the
   subset where the knowledge base was wrong the first time, with a paired bootstrap interval for combined - kb.

Query sets: ``recurrence`` (default; dev incidents re-generated on a different service, ``data/make_recurrences.py``)
and ``test`` (the real test split, almost all of it new failure classes: a control for false recall; opening it is
logged in ``eval/test_openings.log``).

Usage: python -m incident_agent.eval.memory_eval --queries recurrence --tag s12_hash
The recurrences are synthetic variants of the same generator, so absolute numbers are an upper bound.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_agent.eval.compare import paired_bootstrap
from incident_agent.eval.harness import RESULTS_DIR, ROOT, SPLITS_FILE, _log_test_opening, load_split
from incident_agent.eval.retrieval_eval import GOLD_FILE, load_gold
from incident_agent.knowledge.corpus import load_corpus
from incident_agent.knowledge.embedders import Embedder, EmbedderError, build_embedder
from incident_agent.knowledge.hybrid import HybridSearcher
from incident_agent.knowledge.query import INCIDENTS_DIR, QueryError, build_log_query
from incident_agent.knowledge.vector_store import VectorStoreError, build_store
from incident_agent.memory.episodic import EpisodeMemoryError, EpisodicMemory

RECURRENCE_DIR = ROOT / "data" / "recurrence" / "incidents"
RECURRENCE_GOLD = ROOT / "eval" / "recurrence_gold.json"


# Chosen on dev only: top-episode cosine was <= 0.44 for 14 dev incidents with no matching episode (leave-one-out)
# and >= 0.74 for 13 of 14 recurrences (one at 0.41 is missed). Only meaningful for the hashing embedder; re-derive
# it for another embedder with a dev-only run before trusting it.
DEFAULT_MIN_SIMILARITY = 0.6


class MemoryEvalError(ValueError):
    pass


def kb_predict(kb: HybridSearcher, query: str, k: int = 5) -> str | None:
    """Failure class of the best-ranked knowledge-base document that has one."""
    for hit in kb.search(query, k, "hybrid"):
        if hit.failure_class:
            return hit.failure_class
    return None


def seed_memory(
    memory: EpisodicMemory,
    kb: HybridSearcher,
    ids: Sequence[str],
    gold: Mapping[str, Mapping[str, Any]],
    incidents_dir: Path,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Replay history in order: predict first, write the resolved episode afterwards."""
    log: list[dict[str, Any]] = []
    for iid in ids:
        if iid not in gold:
            raise MemoryEvalError(f"no retrieval gold for {iid}")
        query = build_log_query(iid, incidents_dir)
        diagnosed = kb_predict(kb, query)
        recalled = memory.predict(query, k).label  # computed before the episode exists
        resolved = gold[iid]["primary"]
        memory.record(
            iid,
            query,
            resolved,
            diagnosed_class=diagnosed,
            recalled_class=recalled,
            is_false_alarm=str(resolved).startswith("false_alarm"),
        )
        log.append({"incident_id": iid, "diagnosed": diagnosed, "resolved": resolved, "recalled": recalled})
    return log


def evaluate(
    memory: EpisodicMemory,
    kb: HybridSearcher,
    queries: Mapping[str, Mapping[str, Any]],
    query_dir: Path,
    k: int = 3,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> dict[str, Any]:
    history_classes = {e.resolved_class for e in memory.episodes}
    first_wrong = {e.incident_id for e in memory.episodes if e.diagnosis_correct is False}
    rows: list[dict[str, Any]] = []
    for iid in sorted(queries):
        truth = queries[iid]["primary"]
        q = build_log_query(iid, query_dir)
        kb_label = kb_predict(kb, q)
        pred = memory.predict(q, k)
        use_memory = pred.label is not None and pred.similarity >= min_similarity
        combined = pred.label if use_memory else kb_label
        origin = queries[iid].get("recurrence_of")
        rows.append(
            {
                "incident_id": iid,
                "recurrence_of": origin,
                "truth": truth,
                "novel": truth not in history_classes,
                "kb": kb_label,
                "memory": pred.label,
                "memory_similarity": round(pred.similarity, 4),
                "combined": combined,
                "kb_correct": kb_label == truth,
                "memory_answered": use_memory,
                "memory_correct": use_memory and pred.label == truth,
                "combined_correct": combined == truth,
                "first_encounter_kb_wrong": origin in first_wrong,
                "recalled": [r.episode.incident_id for r in pred.recollections],
            }
        )
    return {"summary": summarise(rows), "incidents": rows}


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    answered = [r for r in rows if r["memory_answered"]]
    diffs = [float(r["combined_correct"]) - float(r["kb_correct"]) for r in rows]
    lo, hi = paired_bootstrap(diffs)
    wrong_first = [r for r in rows if r["first_encounter_kb_wrong"]]
    novel = [r for r in rows if r["novel"]]
    return {
        "n": n,
        "kb_accuracy": _mean([float(r["kb_correct"]) for r in rows]),
        "combined_accuracy": _mean([float(r["combined_correct"]) for r in rows]),
        "memory_coverage": _mean([float(r["memory_answered"]) for r in rows]),
        "memory_precision": _mean([float(r["memory_correct"]) for r in answered]),
        "rescued": sum(r["combined_correct"] and not r["kb_correct"] for r in rows),
        "misled": sum(r["kb_correct"] and not r["combined_correct"] for r in rows),
        "delta_combined_minus_kb": {
            "mean": _mean(diffs),
            "ci": (round(lo, 4), round(hi, 4)),
        },
        "first_encounter_kb_wrong": {
            "n": len(wrong_first),
            "kb_accuracy": _mean([float(r["kb_correct"]) for r in wrong_first]),
            "combined_accuracy": _mean([float(r["combined_correct"]) for r in wrong_first]),
        },
        "novel_classes": {
            "n": len(novel),
            "memory_answered": sum(r["memory_answered"] for r in novel),
            "kb_accuracy": _mean([float(r["kb_correct"]) for r in novel]),
            "combined_accuracy": _mean([float(r["combined_correct"]) for r in novel]),
        },
    }


def format_report(report: Mapping[str, Any]) -> str:
    s = report["summary"]
    d = s["delta_combined_minus_kb"]
    fw, nv = s["first_encounter_kb_wrong"], s["novel_classes"]
    return "\n".join(
        [
            f"queries: {s['n']}   kb accuracy {s['kb_accuracy']:.3f}   combined accuracy {s['combined_accuracy']:.3f}",
            f"memory coverage {s['memory_coverage']:.3f}   memory precision when answering {s['memory_precision']:.3f}",
            f"rescued {s['rescued']}  misled {s['misled']}   combined-kb {d['mean']:+.3f} CI {tuple(d['ci'])}",
            f"first encounter diagnosed wrong by kb: n={fw['n']}  kb {fw['kb_accuracy']:.3f} -> combined "
            f"{fw['combined_accuracy']:.3f}",
            f"novel classes (not in memory): n={nv['n']}  memory answered {nv['memory_answered']}  "
            f"kb {nv['kb_accuracy']:.3f}  combined {nv['combined_accuracy']:.3f}",
        ]
    )


def result_path(tag: str | None, results_dir: Path | None = None) -> Path:
    return (results_dir or RESULTS_DIR) / (f"memory__{tag}.json" if tag else "memory.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate episodic memory on recurring incidents")
    ap.add_argument("--queries", default="recurrence", choices=["recurrence", "test"])
    ap.add_argument("--embedder", default=None, help="hashing | st:<model> (default: $EMBEDDER or hashing)")
    ap.add_argument("--store", default=None, choices=["numpy", "qdrant"])
    ap.add_argument("--k", type=int, default=3, help="episodes recalled per query")
    ap.add_argument(
        "--min-similarity",
        type=float,
        default=DEFAULT_MIN_SIMILARITY,
        help="dense cosine the best episode needs for memory to override the kb",
    )
    ap.add_argument("--memory-dir", default=None, help="persist episodes here (default: in memory only)")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args(argv)

    try:
        if not 0.0 <= args.min_similarity <= 1.0:
            raise MemoryEvalError("--min-similarity must be between 0 and 1")
        history_gold = load_gold(GOLD_FILE)
        embedder: Embedder = build_embedder(args.embedder)
        kb = HybridSearcher(embedder, build_store(args.store))
        kb.index(load_corpus())
        memory = EpisodicMemory(embedder, args.memory_dir, build_store(args.store))
        if len(memory):
            raise MemoryEvalError(f"--memory-dir already holds {len(memory)} episodes; use an empty directory")
        history_ids = load_split("dev", SPLITS_FILE)
        if args.queries == "test":
            query_ids = load_split("test", SPLITS_FILE)
            queries = {i: history_gold[i] for i in query_ids}
            query_dir = INCIDENTS_DIR
            _log_test_opening("memory", args.tag, query_ids)
            print("NOTE: TEST SET OPENED (logged in eval/test_openings.log). Do not tune on these results.")
        else:
            queries = json.loads(RECURRENCE_GOLD.read_text())["incidents"]
            query_dir = RECURRENCE_DIR
        seeding = seed_memory(memory, kb, history_ids, history_gold, INCIDENTS_DIR, args.k)
        report = evaluate(memory, kb, queries, query_dir, args.k, args.min_similarity)
    except (EmbedderError, VectorStoreError, EpisodeMemoryError, MemoryEvalError, QueryError, OSError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 2
    report["seeding"] = seeding
    report["meta"] = {
        "tag": args.tag,
        "queries": args.queries,
        "embedder": embedder.name,
        "k": args.k,
        "min_similarity": args.min_similarity,
        "n_episodes": len(memory),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out = result_path(args.tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    wrong = sum(e["diagnosed"] != e["resolved"] for e in seeding)
    print(f"embedder={embedder.name} episodes={len(memory)} (kb misdiagnosed {wrong}/{len(seeding)} first encounters)")
    print(format_report(report))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
