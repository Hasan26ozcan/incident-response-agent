# ruff: noqa: E501
"""Campaigns: named sets of configurations to measure, and a plan that says what will be spent before anything runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incident_agent.eval import harness
from incident_agent.lab.runner import LAB_DATA

SETS = ("dev", "lab")
# Rough LLM calls per incident, from earlier runs of each agent design; used ONLY to size a plan, never reported as a result.
CALLS_PER_INCIDENT = {
    "llm_react": 6,
    "llm_orchestrator": 12,
    "llm_swarm": 14,
    "llm_debate": 16,
    "llm_tot": 18,
    "llm_swarm_full": 24,
}

G20, G120, MISTRAL = "groq/openai/gpt-oss-20b", "groq/openai/gpt-oss-120b", "mistral/mistral-large-latest"


@dataclass(frozen=True)
class Config:
    name: str
    agent: str
    model: str  # spec from measure.spec


CAMPAIGNS: dict[str, tuple[Config, ...]] = {
    "smoke": (Config("react-120b", "llm_react", G120),),
    "models": (
        Config("react-20b", "llm_react", G20),
        Config("react-120b", "llm_react", G120),
        Config("react-mistral-large", "llm_react", MISTRAL),
    ),
    "cascade": (
        Config("react-20b", "llm_react", G20),
        Config("react-120b", "llm_react", G120),
        Config("react-cascade", "llm_react", f"cascade:{G20}>{G120}"),
    ),
    "agents": (
        Config("react-120b", "llm_react", G120),
        Config("orchestrator-120b", "llm_orchestrator", G120),
        Config("swarm-120b", "llm_swarm", G120),
    ),
}


def set_ids(name: str, *, lab_dir: Path = LAB_DATA, splits_file: Path | None = None) -> list[str]:
    if name == "dev":
        return harness.load_split("dev", splits_file)
    if name == "lab":
        return sorted(p.stem for p in (lab_dir / "gold").glob("LAB-*.json"))
    raise ValueError(f"unknown set {name!r}: choose from {SETS} (the test split needs the explicit milestone flag)")


def plan(campaign: str, sets: list[str]) -> dict[str, Any]:
    configs = CAMPAIGNS[campaign]
    n = {s: len(set_ids(s)) for s in sets}
    total_incidents = sum(n.values())
    rows: list[dict[str, Any]] = []
    for c in configs:
        est = CALLS_PER_INCIDENT.get(c.agent, 10) * total_incidents * (1.3 if c.model.startswith("cascade:") else 1.0)
        rows.append(
            {
                "config": c.name,
                "agent": c.agent,
                "model": c.model,
                "incidents": total_incidents,
                "est_llm_calls": int(est),
            }
        )
    return {
        "campaign": campaign,
        "sets": n,
        "configs": rows,
        "est_total_calls": sum(int(r["est_llm_calls"]) for r in rows),
    }


def render_plan(p: dict[str, Any]) -> str:
    lines = [
        f"campaign {p['campaign']}: sets {p['sets']}",
        "estimate only (calls per incident differ by agent and incident):",
    ]
    for r in p["configs"]:
        lines.append(f"  {r['config']:<22} {r['agent']:<18} {r['model']:<60} ~{r['est_llm_calls']} calls")
    lines.append(
        f"  total ~{p['est_total_calls']} calls; free tiers are rate limited, so runs are resumable (--resume) and paced"
    )
    return "\n".join(lines)


def dumps(p: dict[str, Any]) -> str:
    return json.dumps(p, indent=2)
