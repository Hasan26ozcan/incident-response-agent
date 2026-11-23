# ruff: noqa: E501
"""Score any diagnosis agent on the lab incidents (real captured processes), using the same scorer as the dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from incident_agent.eval import harness
from incident_agent.lab.runner import LAB_DATA


def evaluate_lab(
    agent: str = "react_baseline",
    ids: list[str] | None = None,
    *,
    data: Path = LAB_DATA,
    temperature: float = 0.0,
    diagnose: Any = None,
) -> dict[str, Any]:
    fn = diagnose or harness.make_adapter(agent, temperature)
    report = harness.evaluate(fn, gold_dir=data / "gold", ids=ids, pattern="LAB-*.json")
    report["meta"] = {"agent": agent, "source": "fault-lab", "n_incidents": report["summary"]["n"]}
    return report


def save(report: dict[str, Any], out_dir: Path | None = None) -> Path:
    d = out_dir or harness.RESULTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"lab__{report['meta']['agent']}.json"
    p.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return p
