"""Run one configuration over one incident set with a real (or injected) client; save after every incident."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_agent.eval import harness
from incident_agent.lab.runner import LAB_DATA
from incident_agent.measure import spec as spec_mod
from incident_agent.measure.campaign import Config, set_ids
from incident_agent.observe import report as obs_report
from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.trace import JsonlSink, MemorySink, Tracer, read_spans

ADAPTER_FACTORY = Callable[[str, Any], Callable[[str], harness.DiagnosisResult]]


def default_adapter(agent: str, client: Any) -> Callable[[str], harness.DiagnosisResult]:
    with harness.use_client(client):
        return harness.make_adapter(agent, 0.0)


def result_file(out: Path, config: Config, set_name: str) -> Path:
    return out / f"{config.name}__{set_name}.json"


def _incident_cost(spans: list[Any], trace_id: str, prices: PriceTable) -> dict[str, Any]:
    rows = [
        {
            "model": s.attrs.get("model", "?"),
            "prompt_tokens": s.attrs.get("prompt_tokens", 0),
            "completion_tokens": s.attrs.get("completion_tokens", 0),
            "cached_tokens": s.attrs.get("cached_tokens", 0),
        }
        for s in spans
        if s.trace_id == trace_id and s.kind == "llm" and "prompt_tokens" in s.attrs
    ]
    cost = prices.summarize(rows)
    return {
        "calls": len(rows),
        "cost_usd": cost["total_usd"],
        "unpriced_calls": cost["unpriced_calls"],
        "by_model": cost["by_model"],
        "latency_s": round(
            sum(s.duration for s in spans if s.trace_id == trace_id and s.kind == "llm" and "prompt_tokens" in s.attrs),
            3,
        ),
    }


def run_config(
    config: Config,
    set_name: str,
    out: Path,
    *,
    resume: bool = False,
    ids: list[str] | None = None,
    client_factory: Callable[[str, Tracer], Any] | None = None,
    adapter_factory: ADAPTER_FACTORY = default_adapter,
    lab_dir: Path = LAB_DATA,
    prices: PriceTable | None = None,
    pace: float = 0.0,
) -> Path:
    prices = prices or PriceTable.load()
    out.mkdir(parents=True, exist_ok=True)
    path = result_file(out, config, set_name)
    trace_file = out / f"{config.name}__{set_name}.trace.jsonl"
    gold_dir, pattern = (lab_dir / "gold", "LAB-*.json") if set_name == "lab" else (None, "INC-*.json")
    want = ids or set_ids(set_name, lab_dir=lab_dir)
    done: dict[str, harness.IncidentScore] = {}
    extras: dict[str, dict[str, Any]] = {}
    if resume and path.exists():
        old = json.loads(path.read_text())
        extras = dict(old.get("measure", {}))
        for row in old["incidents"]:
            if not row["error"]:
                done[row["incident_id"]] = harness.IncidentScore(**row)
    elif trace_file.exists():
        trace_file.unlink()
    mem = MemorySink()
    tracer = Tracer([JsonlSink(trace_file), mem])
    client = (client_factory or spec_mod.build_client)(config.model, tracer)
    diagnose = adapter_factory(config.agent, client)
    meta = {
        "config": config.name,
        "agent": config.agent,
        "model": config.model,
        "set": set_name,
        "incidents": want,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "temperature": 0.0,
        "cache": "off",
    }
    scores: list[harness.IncidentScore] = list(done.values())

    def timed(incident_id: str) -> harness.DiagnosisResult:
        t0 = time.perf_counter()
        with tracer.span(f"incident:{incident_id}", "run", incident=incident_id, config=config.name) as sp:
            res = diagnose(incident_id)
        extras[incident_id] = {
            **_incident_cost(mem.spans, sp.trace_id, prices),
            "wall_s": round(time.perf_counter() - t0, 3),
        }
        if pace:
            time.sleep(pace)
        return res

    def save(complete: bool) -> None:
        body = {
            "meta": {**meta, "complete": complete},
            "summary": harness.summarize(scores),
            "incidents": [s.__dict__ for s in scores],
            "measure": extras,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        tmp.replace(path)

    def on_result(score: harness.IncidentScore, res: harness.DiagnosisResult) -> None:
        scores.append(score)
        save(False)  # survives rate-limit cut-offs and Ctrl+C

    rep = harness.evaluate(timed, gold_dir=gold_dir, ids=want, on_result=on_result, skip=done, pattern=pattern)
    scores[:] = [harness.IncidentScore(**r) for r in rep["incidents"]]
    save(True)
    spans = read_spans(trace_file)
    (out / f"{config.name}__{set_name}.observability.html").write_text(
        obs_report.render_dashboard(obs_report.summarize(spans, prices), f"{config.name} on {set_name}"),
        encoding="utf-8",
    )
    return path
