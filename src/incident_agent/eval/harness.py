"""R0/R1 eval harness: scores any diagnoser against eval/gold/*.json.

Deterministic proxy metrics need zero LLM calls, so this is also the regression gate in CI.
Features: frozen dev/test split, tagged result files, incremental saving (+ --resume), per-incident
traces, token/call accounting, crash reporting, and an audit log of every test-set opening.
Adapter contract: a diagnoser is a callable ``incident_id -> DiagnosisResult``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
GOLD_DIR = ROOT / "eval" / "gold"
RESULTS_DIR = ROOT / "eval" / "results"
TRACE_DIR = ROOT / "eval" / "traces"
SPLITS_FILE = ROOT / "eval" / "splits.json"
OPENINGS_LOG = ROOT / "eval" / "test_openings.log"

_STOP = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "into",
    "per",
    "its",
    "has",
    "have",
    "then",
    "than",
    "not",
    "but",
    "all",
    "any",
    "can",
    "will",
    "starting",
}
_FALSE_ALARM_MARKERS = ("false alarm", "no incident", "no real incident", "not an incident")
_TAG = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class DiagnosisResult:
    """Framework-neutral diagnosis, so any agent version can be scored."""

    incident_id: str
    root_cause: str
    evidence: list[str] = field(default_factory=list)
    risk_tier: str = "unknown"
    steps: int = 0
    error: str = ""  # set by an adapter that caught a crash but still has a partial trace
    trace: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    degraded: str = ""  # non-empty when the agent returned a flagged fallback instead of a real diagnosis


@dataclass
class IncidentScore:
    incident_id: str
    category: str
    difficulty: str
    root_cause_f1: float
    evidence_recall: float
    risk_match: bool
    false_alarm_ok: bool
    answered: bool  # produced any root cause other than "Unknown"
    error: str = ""  # non-empty when the agent crashed on this incident
    usage: dict[str, int] = field(default_factory=dict)
    degraded: str = ""


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]{3,}", text.lower()) if t not in _STOP}


def f1_overlap(pred: str, ref: str) -> float:
    p, r = _tokens(pred), _tokens(ref)
    if not p or not r:
        return 0.0
    common = len(p & r)
    if common == 0:
        return 0.0
    prec, rec = common / len(p), common / len(r)
    return 2 * prec * rec / (prec + rec)


def evidence_recall(pred: list[str], gold: list[str]) -> float:
    """Share of gold evidence items whose key tokens appear in the predicted evidence."""
    if not gold:
        return 1.0
    pred_tokens = _tokens(" ".join(pred))
    hit = 0
    for g in gold:
        gt = _tokens(g)
        if gt and len(gt & pred_tokens) / len(gt) >= 0.5:
            hit += 1
    return hit / len(gold)


def score_one(gold: dict, result: DiagnosisResult, error: str = "") -> IncidentScore:
    claims_false_alarm = any(m in result.root_cause.lower() for m in _FALSE_ALARM_MARKERS)
    is_fa = bool(gold.get("is_false_alarm"))
    return IncidentScore(
        incident_id=gold["incident_id"],
        category=gold["category"],
        difficulty=gold["difficulty"],
        root_cause_f1=round(f1_overlap(result.root_cause, gold["root_cause"]), 3),
        evidence_recall=round(evidence_recall(result.evidence, gold["evidence"]), 3),
        risk_match=result.risk_tier == gold["remediation_risk_tier"],
        false_alarm_ok=claims_false_alarm == is_fa,
        answered=not result.root_cause.lower().startswith("unknown"),
        error=error or result.error,
        usage=result.usage,
        degraded=result.degraded,
    )


def summarize(scores: list[IncidentScore]) -> dict[str, Any]:
    n = len(scores) or 1
    return {
        "n": len(scores),
        "root_cause_f1": round(sum(s.root_cause_f1 for s in scores) / n, 3),
        "evidence_recall": round(sum(s.evidence_recall for s in scores) / n, 3),
        "risk_accuracy": round(sum(s.risk_match for s in scores) / n, 3),
        "false_alarm_accuracy": round(sum(s.false_alarm_ok for s in scores) / n, 3),
        "answered_rate": round(sum(s.answered for s in scores) / n, 3),
        "crashed": sum(bool(s.error) for s in scores),
        "degraded": sum(bool(s.degraded) for s in scores),
        "llm_calls": sum(s.usage.get("calls", 0) for s in scores),
        "prompt_tokens": sum(s.usage.get("prompt_tokens", 0) for s in scores),
        "completion_tokens": sum(s.usage.get("completion_tokens", 0) for s in scores),
    }


def load_split(name: str, splits_file: Path | None = None) -> list[str]:
    data = json.loads((splits_file or SPLITS_FILE).read_text())
    if name not in ("dev", "test"):
        raise ValueError(f"unknown split {name!r} (use dev, test or all)")
    return list(data[name])


def evaluate(
    diagnose: Callable[[str], DiagnosisResult],
    gold_dir: Path | None = None,
    limit: int | None = None,
    ids: list[str] | None = None,
    on_result: Callable[[IncidentScore, DiagnosisResult], None] | None = None,
    skip: dict[str, IncidentScore] | None = None,
) -> dict:
    """Run `diagnose` over gold incidents. `skip` holds already-finished scores (for --resume)."""
    paths = sorted((gold_dir or GOLD_DIR).glob("INC-*.json"))
    if ids:
        known = {p.stem for p in paths}
        unknown = sorted(set(ids) - known)
        if unknown:
            raise ValueError(f"unknown incident ids: {', '.join(unknown)}")
        paths = [p for p in paths if p.stem in set(ids)]
    paths = paths[:limit]
    scores: list[IncidentScore] = []
    for path in paths:
        gold = json.loads(path.read_text())
        iid = gold["incident_id"]
        if skip and iid in skip:
            scores.append(skip[iid])
            continue
        error = ""
        try:
            res = diagnose(iid)
        except Exception as exc:  # a crash is a failed incident, not a skipped one
            error = f"{type(exc).__name__}: {exc}"
            res = DiagnosisResult(iid, f"Unknown (error: {exc})")
        score = score_one(gold, res, error)
        scores.append(score)
        if on_result:
            on_result(score, res)
    return {"summary": summarize(scores), "incidents": [asdict(s) for s in scores]}


def react_baseline(incident_id: str) -> DiagnosisResult:
    """Adapter for the legacy rule-based ReActAgent (Stage 3)."""
    from incident_agent.agents.react_agent import ReActAgent

    agent = ReActAgent()
    obs = agent.observe(incident_id)
    d = agent.act(obs, agent.reason(obs))
    return DiagnosisResult(
        incident_id=incident_id,
        root_cause=d.root_cause,
        evidence=[e.detail for e in d.evidence],
        risk_tier=getattr(d.risk_tier, "value", str(d.risk_tier)),
        steps=len(d.reasoning_steps),
    )


def make_llm_react(temperature: float = 0.0) -> Callable[[str], DiagnosisResult]:
    """Adapter factory for the LLM ReAct agent (needs GROQ_API_KEY; responses are disk-cached)."""
    from incident_agent.agents.llm_react_agent import LLMReActAgent
    from incident_agent.llm import TrackingClient, build_client

    shared = build_client()  # shared so cache/fallback state spans the whole run

    def run(incident_id: str) -> DiagnosisResult:
        client = TrackingClient(shared)
        agent = LLMReActAgent(client, temperature=temperature)
        trace: dict[str, Any] = {}
        try:
            d = agent.run(incident_id)
        except Exception as exc:  # keep the partial trace: it is the best debugging evidence
            trace = _agent_trace(agent)
            return DiagnosisResult(
                incident_id,
                f"Unknown (error: {exc})",
                error=f"{type(exc).__name__}: {exc}",
                trace=trace,
                usage=client.summary(),
            )
        trace = _agent_trace(agent)
        trace["diagnosis"] = d.model_dump(mode="json")
        return DiagnosisResult(
            incident_id=incident_id,
            root_cause=d.root_cause,
            evidence=[e.detail for e in d.evidence],
            risk_tier=d.risk_tier.value,
            steps=len(d.reasoning_steps),
            trace=trace,
            usage=client.summary(),
            degraded=agent.degraded or "",
        )

    return run


def _agent_trace(agent: Any) -> dict[str, Any]:
    return {
        "model": getattr(agent.client, "model", "unknown"),
        "plan_history": [p.model_dump() for p in agent.plan_history],
        "transcript": list(agent.transcript),
        "unavailable_tools": sorted(agent.unavailable),
        "degraded": getattr(agent, "degraded", None),
    }


ADAPTER_NAMES = ("react_baseline", "llm_react")


def make_adapter(name: str, temperature: float = 0.0) -> Callable[[str], DiagnosisResult]:
    if name == "react_baseline":
        return react_baseline
    if name == "llm_react":
        return make_llm_react(temperature)
    raise ValueError(f"unknown agent {name!r}; choose from {ADAPTER_NAMES}")


def result_path(agent: str, tag: str | None) -> Path:
    if tag is not None and not _TAG.match(tag):
        raise ValueError("--tag may contain only letters, digits, '_', '-' and '.'")
    return RESULTS_DIR / (f"{agent}__{tag}.json" if tag else f"{agent}.json")


def _write_report(path: Path, meta: dict[str, Any], scores: list[IncidentScore], complete: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "meta": {**meta, "complete": complete},
        "summary": summarize(scores),
        "incidents": [asdict(s) for s in scores],
    }
    path.write_text(json.dumps(report, indent=2))


def _log_test_opening(agent: str, tag: str | None, ids: list[str]) -> None:
    OPENINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with OPENINGS_LOG.open("a") as fh:
        fh.write(f"{stamp}\tagent={agent}\ttag={tag or '-'}\tn={len(ids)}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a diagnoser against the gold set")
    ap.add_argument("--agent", default="react_baseline", choices=ADAPTER_NAMES)
    ap.add_argument("--split", default="all", choices=["all", "dev", "test"])
    ap.add_argument("--ids", default=None, help="comma-separated incident ids (overrides --split)")
    ap.add_argument("--limit", type=int, default=None, help="score only the first N selected incidents")
    ap.add_argument("--tag", default=None, help="label for the run; result goes to <agent>__<tag>.json")
    ap.add_argument("--trace", action="store_true", help="save per-incident traces under eval/traces/")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--resume", action="store_true", help="skip incidents already finished in the result file")
    args = ap.parse_args(argv)

    ids: list[str] | None
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    elif args.split != "all":
        ids = load_split(args.split)
    else:
        ids = None
    opens_test = args.split == "test" and not args.ids
    if opens_test:
        _log_test_opening(args.agent, args.tag, ids or [])
        print("NOTE: TEST SET OPENED (logged in eval/test_openings.log). Do not tune on these results.")

    out = result_path(args.agent, args.tag)
    meta: dict[str, Any] = {
        "agent": args.agent,
        "tag": args.tag,
        "split": "ids" if args.ids else args.split,
        "temperature": args.temperature,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    done: dict[str, IncidentScore] = {}
    if args.resume and out.exists():
        for row in json.loads(out.read_text())["incidents"]:
            if not row["error"]:
                done[row["incident_id"]] = IncidentScore(**row)
        print(f"resuming: {len(done)} incident(s) already finished")

    diagnose = make_adapter(args.agent, args.temperature)
    trace_dir = TRACE_DIR / (args.tag or args.agent)
    scores_so_far: list[IncidentScore] = list(done.values())

    def on_result(score: IncidentScore, res: DiagnosisResult) -> None:
        scores_so_far.append(score)
        _write_report(out, meta, scores_so_far, complete=False)  # survives quota cut-offs
        if args.trace and res.trace:
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / f"{score.incident_id}.json").write_text(json.dumps(res.trace, indent=2))

    report = evaluate(diagnose, ids=ids, limit=args.limit, on_result=on_result, skip=done)
    final = [IncidentScore(**r) for r in report["incidents"]]
    _write_report(out, meta, final, complete=True)

    for r in report["incidents"]:
        line = f"{r['incident_id']} {r['category']:<22} f1={r['root_cause_f1']:.2f} risk={r['risk_match']}"
        print(line + (f"  CRASHED -> {r['error'][:300]}" if r["error"] else ""))
    print(json.dumps(report["summary"], indent=2))
    if report["summary"]["crashed"]:
        n = report["summary"]["crashed"]
        print(f"\nWARNING: {n} incident(s) crashed - these scores are meaningless until fixed.")
    print(f"saved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
