"""R0 eval harness: scores any diagnoser against eval/gold/*.json.

Runs with zero LLM calls (deterministic proxy metrics), so it can be the
regression gate in CI. An LLM-as-judge can be plugged in later via `judge`.
Adapter contract: a diagnoser is a callable ``incident_id -> DiagnosisResult``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from incident_agent.llm import LLMClient

ROOT = Path(__file__).resolve().parents[3]
GOLD_DIR = ROOT / "eval" / "gold"
RESULTS_DIR = ROOT / "eval" / "results"

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


@dataclass
class DiagnosisResult:
    """Framework-neutral diagnosis, so any agent version can be scored."""

    incident_id: str
    root_cause: str
    evidence: list[str] = field(default_factory=list)
    risk_tier: str = "unknown"
    steps: int = 0


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


def score_one(gold: dict, result: DiagnosisResult) -> IncidentScore:
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
    )


def evaluate(diagnose: Callable[[str], DiagnosisResult], gold_dir: Path = GOLD_DIR) -> dict:
    scores: list[IncidentScore] = []
    for path in sorted(gold_dir.glob("INC-*.json")):
        gold = json.loads(path.read_text())
        try:
            res = diagnose(gold["incident_id"])
        except Exception as exc:  # a crash is a failed incident, not a skipped one
            res = DiagnosisResult(gold["incident_id"], f"Unknown (error: {exc})")
        scores.append(score_one(gold, res))
    n = len(scores) or 1
    summary = {
        "n": len(scores),
        "root_cause_f1": round(sum(s.root_cause_f1 for s in scores) / n, 3),
        "evidence_recall": round(sum(s.evidence_recall for s in scores) / n, 3),
        "risk_accuracy": round(sum(s.risk_match for s in scores) / n, 3),
        "false_alarm_accuracy": round(sum(s.false_alarm_ok for s in scores) / n, 3),
        "answered_rate": round(sum(s.answered for s in scores) / n, 3),
    }
    return {"summary": summary, "incidents": [asdict(s) for s in scores]}


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


def llm_react(incident_id: str) -> DiagnosisResult:
    """Adapter for the LLM ReAct agent (needs GROQ_API_KEY; responses are disk-cached)."""
    from incident_agent.agents.llm_react_agent import LLMReActAgent
    from incident_agent.llm import build_client

    global _CLIENT
    if _CLIENT is None:
        _CLIENT = build_client()
    d = LLMReActAgent(_CLIENT).run(incident_id)
    return DiagnosisResult(
        incident_id=incident_id,
        root_cause=d.root_cause,
        evidence=[e.detail for e in d.evidence],
        risk_tier=d.risk_tier.value,
        steps=len(d.reasoning_steps),
    )


_CLIENT: LLMClient | None = None  # shared so the cache/fallback state spans the whole eval run

ADAPTERS: dict[str, Callable[[str], DiagnosisResult]] = {"react_baseline": react_baseline, "llm_react": llm_react}


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a diagnoser against the gold set")
    ap.add_argument("--agent", default="react_baseline", choices=sorted(ADAPTERS))
    args = ap.parse_args()
    report = evaluate(ADAPTERS[args.agent])
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{args.agent}.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
