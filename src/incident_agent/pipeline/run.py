# ruff: noqa: E501
"""investigate(): one incident through the whole system.

triage -> diagnose (real agent, traced, hardened) -> plan_action (allow-list + policy) -> approval wait -> execute -> report.
``shadow`` mode (default) never honours an approval: the wait times out and the proposed command is dry-run only.
``assist`` leaves the run waiting for a human approval sealed by the HITL gate (use the durable/hitl tools to resume).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incident_agent.durable.engine import Engine
from incident_agent.durable.workflows import Deps, incident_workflow
from incident_agent.observe import report as obs_report
from incident_agent.observe.journal_traces import spans_from_events
from incident_agent.observe.trace import JsonlSink, Tracer, read_spans
from incident_agent.pipeline.context import related_knowledge
from incident_agent.pipeline.diagnoser import make_diagnoser
from incident_agent.pipeline.stack import StackConfig, build_stack

ID_RE = re.compile(r"(INC|LAB)-\d{3}")
MODES = ("shadow", "assist")
SHADOW_TIMEOUT = 0.5


@dataclass
class PipelineConfig:
    agent: str = "llm_react"
    mode: str = "shadow"
    root: Path = field(default_factory=lambda: Path(".cache/pipeline"))
    harden: bool = True
    stack: StackConfig = field(default_factory=StackConfig)
    assist_timeout: float = 900.0


@dataclass
class Investigation:
    run_id: str
    status: str
    diagnosis: dict[str, Any]
    plan: dict[str, Any]
    execution: dict[str, Any]
    summary: str
    related: list[dict[str, Any]]
    observability: dict[str, Any]
    run_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in self.__dict__.items()}


def investigate(
    incident_id: str,
    cfg: PipelineConfig | None = None,
    *,
    client: Any = None,
    backend_factory: Callable[[], Any] | None = None,
    run_id: str | None = None,
) -> Investigation:
    cfg = cfg or PipelineConfig()
    if not ID_RE.fullmatch(incident_id):
        raise ValueError(f"not an incident id: {incident_id!r}")
    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    run_id = run_id or f"{incident_id.lower()}-{int(time.time())}"
    run_dir = cfg.root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tracer = Tracer([JsonlSink(run_dir / "trace.jsonl")])
    stacked = build_stack(tracer, cfg.stack, base=client)
    deps = Deps(
        diagnoser=make_diagnoser(cfg.agent, stacked, harden=cfg.harden),
        approval_timeout=SHADOW_TIMEOUT if cfg.mode == "shadow" else cfg.assist_timeout,
    )
    if backend_factory is not None:
        deps.backend_factory = backend_factory
    engine = Engine(incident_workflow(deps), cfg.root, poll_interval=0.1)
    with tracer.span(f"investigate:{incident_id}", "run", incident=incident_id, agent=cfg.agent, mode=cfg.mode):
        engine.start(run_id, {"incident_id": incident_id})
        state = engine.run(run_id, block=cfg.mode == "shadow")
    related = related_knowledge(incident_id)
    spans = read_spans(run_dir / "trace.jsonl") + spans_from_events(run_id, engine._journal(run_id).events())  # noqa: SLF001
    summary = obs_report.summarize(spans)
    (run_dir / "observability.html").write_text(
        obs_report.render_dashboard(summary, f"{incident_id} - {cfg.agent}"), encoding="utf-8"
    )
    (run_dir / "observability.md").write_text(obs_report.render_markdown(summary), encoding="utf-8")
    r = state.results
    inv = Investigation(
        run_id=run_id,
        status=state.status,
        diagnosis=r.get("diagnose") or {},
        plan=r.get("plan_action") or {},
        execution=r.get("execute") or {},
        summary=(r.get("report") or {}).get("summary", state.error or f"waiting for {state.waiting_for}"),
        related=related,
        observability={k: summary[k] for k in ("llm", "cost", "cascade", "errors")},
        run_dir=run_dir,
    )
    (run_dir / "investigation.json").write_text(json.dumps(inv.to_dict(), indent=2, default=str), encoding="utf-8")
    return inv
