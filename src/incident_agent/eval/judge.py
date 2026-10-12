"""LLM-as-judge for root-cause correctness, using a DIFFERENT model family than the agent.

Token-overlap F1 punishes correct answers phrased differently and rewards copied vocabulary. The judge
reads only the gold root cause and the candidate root cause (never the agent's evidence or identity)
and says whether the candidate names the same mechanism and trigger. Scores: correct 1.0, partially 0.5,
incorrect 0.0. Judging a model with itself is refused: self-preference inflates scores.

Needs traces (run the harness with --trace). Config: JUDGE_MODEL (+ optional JUDGE_PROVIDER=groq|cerebras,
default groq) and the provider's API key. Example: agent on openai/gpt-oss-120b, judge on a Llama model.

Usage: python -m incident_agent.eval.judge --tag s6_orch [--split dev]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from incident_agent.eval.harness import GOLD_DIR, RESULTS_DIR, TRACE_DIR, load_split
from incident_agent.llm import LLMClient, LLMUnavailable, generate_structured
from incident_agent.llm.client import (
    CEREBRAS_BASE_URL,
    GROQ_BASE_URL,
    OpenAICompatClient,
    _load_dotenv,
)

SCORES = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0}

JUDGE_SYSTEM = """\
You grade an incident-response diagnosis. You see the GOLD root cause and a CANDIDATE root cause only.
Judge by meaning, not wording:
- correct: same underlying mechanism AND same trigger/origin (for a false alarm: both say there is no real incident)
- partially_correct: right component or symptom, but the mechanism or the trigger is missing, vague or wrong
- incorrect: different cause, a downstream symptom named as the cause, or a real incident called a false alarm
  (or the reverse)
Do not reward length or confident tone. Return JSON."""

JUDGE_USER = """\
GOLD root cause: {gold}
GOLD is a false alarm: {false_alarm}

CANDIDATE root cause: {candidate}

Grade the candidate."""


class JudgeVerdict(BaseModel):
    verdict: Literal["correct", "partially_correct", "incorrect"]
    mechanism_match: bool
    trigger_match: bool
    reason: str = Field(min_length=5, max_length=400)


class SameModelError(RuntimeError):
    """The judge would grade its own family; refuse unless explicitly allowed."""


def build_judge_client(agent_model: str | None = None, allow_same: bool = False) -> LLMClient:
    from incident_agent.llm.cache import CachedClient

    _load_dotenv()
    model = os.environ.get("JUDGE_MODEL")
    if not model:
        raise LLMUnavailable("JUDGE_MODEL is not set (e.g. a Llama model on Groq when the agent uses gpt-oss)")
    provider = os.environ.get("JUDGE_PROVIDER", "groq").lower()
    key_env, base = {"groq": ("GROQ_API_KEY", GROQ_BASE_URL), "cerebras": ("CEREBRAS_API_KEY", CEREBRAS_BASE_URL)}.get(
        provider, ("", "")
    )
    if not key_env:
        raise LLMUnavailable(f"unknown JUDGE_PROVIDER {provider!r}; use groq or cerebras")
    key = os.environ.get(key_env)
    if not key:
        raise LLMUnavailable(f"{key_env} is not set")
    agent_model = agent_model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    if model == agent_model and not allow_same:
        raise SameModelError(
            f"judge model equals the agent model ({model}); pick a different family or pass --allow-same-model"
        )
    return CachedClient(OpenAICompatClient(model, key, base), os.environ.get("LLM_CACHE_DIR", ".llm_cache"))


def judge_one(client: LLMClient, gold: dict[str, Any], candidate: str) -> JudgeVerdict:
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": JUDGE_USER.format(
                gold=gold["root_cause"], false_alarm=bool(gold.get("is_false_alarm")), candidate=candidate
            ),
        },
    ]
    return generate_structured(client, messages, JudgeVerdict, max_repairs=2, temperature=0.0)


def judge_run(
    client: LLMClient,
    tag: str,
    ids: list[str] | None = None,
    trace_dir: Path | None = None,
    gold_dir: Path | None = None,
) -> dict[str, Any]:
    tdir = (trace_dir or TRACE_DIR) / tag
    gdir = gold_dir or GOLD_DIR
    if not tdir.is_dir():
        raise FileNotFoundError(f"no traces at {tdir}; re-run the harness with --trace (responses are cached)")
    rows: list[dict[str, Any]] = []
    for tf in sorted(tdir.glob("INC-*.json")):
        iid = tf.stem
        if ids and iid not in ids:
            continue
        trace = json.loads(tf.read_text())
        diag = trace.get("diagnosis")
        gold = json.loads((gdir / f"{iid}.json").read_text())
        if not diag:  # crashed run: nothing to judge, counts as incorrect
            rows.append({"incident_id": iid, "verdict": "incorrect", "score": 0.0, "reason": "no diagnosis (crashed)"})
            continue
        v = judge_one(client, gold, diag["root_cause"])
        rows.append(
            {
                "incident_id": iid,
                "verdict": v.verdict,
                "score": SCORES[v.verdict],
                "mechanism_match": v.mechanism_match,
                "trigger_match": v.trigger_match,
                "reason": v.reason,
            }
        )
    mean = sum(r["score"] for r in rows) / len(rows) if rows else 0.0
    return {
        "meta": {"tag": tag, "judge_model": client.model, "n": len(rows)},
        "summary": {"judge_score": round(mean, 3)},
        "incidents": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM-judge the root causes of a traced run")
    ap.add_argument("--tag", required=True, help="tag used for the harness run (traces in eval/traces/<tag>/)")
    ap.add_argument("--split", default="all", choices=["all", "dev", "test"])
    ap.add_argument("--allow-same-model", action="store_true")
    args = ap.parse_args(argv)
    try:
        client = build_judge_client(allow_same=args.allow_same_model)
    except (LLMUnavailable, SameModelError) as exc:
        print(f"cannot start judge: {exc}")
        return 1
    ids = None if args.split == "all" else load_split(args.split)
    report = judge_run(client, args.tag, ids)
    out = RESULTS_DIR / f"judge__{args.tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    for r in report["incidents"]:
        print(f"{r['incident_id']} {r['verdict']:<18} {r['reason'][:110]}")
    print(f"judge_score={report['summary']['judge_score']} (model {report['meta']['judge_model']}) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
