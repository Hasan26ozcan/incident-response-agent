"""LLM orchestrator-workers (Stage 6): triage lead -> parallel specialist workers -> synthesis.

* Triage (1 LLM call) writes hypotheses and a focus question per worker. It cannot drop workers.
* Each worker (log / metrics / deploy) is a guarded tool loop over ONLY its own read-only tools and
  files a schema-validated ``WorkerReport``; quotes are checked against the worker's own tool output.
* Workers run concurrently (threads: the work is I/O bound LLM calls) and fail independently. One
  failed worker lowers coverage but never loses the incident; if none succeed the run degrades to a
  flagged, zero-confidence diagnosis (never a crash).
* Synthesis (1 LLM call) merges the reports; the shared grounding check validates its evidence against
  the union of all worker transcripts, exactly as for the single-agent baseline.

Config errors (bad key, unknown model) are deliberately NOT swallowed: they must fail loudly.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from incident_agent.agents.grounding import degraded_diagnosis, ground_diagnosis, is_grounded, tokens
from incident_agent.agents.llm_react_agent import LLMDiagnosis
from incident_agent.agents.risk_guard import apply_risk_guard
from incident_agent.agents.tool_loop import ToolLoop
from incident_agent.llm import LLMClient, LLMConfigError, LLMUnavailable, StructuredOutputError, generate_structured
from incident_agent.prompts import orchestrator_prompts as P
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools.toolbox import IncidentToolbox, specs_for

WorkerName = Literal["log", "metrics", "deploy"]
WORKER_TOOLS: dict[str, set[str]] = {
    "log": {"log_overview", "search_logs"},
    "metrics": {"get_metric_summary"},
    "deploy": {"get_recent_deploys"},
}
WORKER_ORDER: tuple[str, ...] = ("log", "metrics", "deploy")
_DEFAULT_FOCUS = "Find what is abnormal in your domain, when it starts, and the earliest likely cause."


class Assignment(BaseModel):
    worker: WorkerName
    focus: str = Field(min_length=3)


class TriagePlan(BaseModel):
    hypotheses: list[str] = Field(min_length=1, max_length=4)
    assignments: list[Assignment] = Field(min_length=1, max_length=3)


class WorkerFact(BaseModel):
    claim: str = Field(min_length=3)
    quote: str = Field(min_length=3)
    timestamp: str = ""


class WorkerReport(BaseModel):
    domain_normal: bool
    summary: str = Field(min_length=10)
    facts: list[WorkerFact] = Field(default_factory=list, max_length=6)
    anomaly_onset: str = ""
    suspected_cause: str = ""
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class WorkerOutcome:
    name: str
    report: WorkerReport | None = None
    transcript: list[str] = field(default_factory=list)
    unavailable: set[str] = field(default_factory=set)
    error: str = ""


def reports_json(outcomes: list[WorkerOutcome], limit: int = 9000) -> str:
    """Worker reports as JSON; every fact carries ``verified`` (quote found in that worker's tool output)."""
    blob: dict[str, Any] = {}
    for o in outcomes:
        if o.report is None:
            continue
        seen = tokens("\n".join(o.transcript))
        d = o.report.model_dump()
        d["facts"] = [{**f, "verified": is_grounded(f["quote"], seen)} for f in d["facts"]]
        blob[o.name] = d
    return json.dumps(blob, indent=1)[:limit]


def _default_triage() -> TriagePlan:
    return TriagePlan(
        hypotheses=["unknown: sweep every domain"],
        assignments=[Assignment(worker=w, focus=_DEFAULT_FOCUS) for w in WORKER_ORDER],  # type: ignore[arg-type]
    )


class LLMOrchestrator:
    agent_type = "llm_orchestrator"

    def __init__(
        self,
        client: LLMClient,
        *,
        parallel: bool = True,
        use_llm_triage: bool = True,
        temperature: float = 0.0,
        max_repairs: int = 2,
        worker_max_steps: int = 5,
        worker_max_tool_calls: int = 6,
        fail_tools: set[str] | None = None,
        fail_workers: set[str] | None = None,
        risk_guard: bool = False,
    ) -> None:
        self.client = client
        self.parallel = parallel
        self.use_llm_triage = use_llm_triage
        self.temperature = temperature
        self.max_repairs = max_repairs
        self.worker_max_steps = worker_max_steps
        self.worker_max_tool_calls = worker_max_tool_calls
        self.fail_tools = fail_tools or set()
        self.fail_workers = fail_workers or set()  # fault injection: these workers raise
        self.risk_guard = risk_guard
        self._reset()

    def _reset(self) -> None:
        self.triage: TriagePlan | None = None
        self.outcomes: dict[str, WorkerOutcome] = {}
        self.degraded: str | None = None
        self.transcript: list[str] = []
        self.unavailable: set[str] = set()  # tools withdrawn in any worker (trace compatibility)
        self.plan_history: list[TriagePlan] = []

    # -- triage ---------------------------------------------------------------
    def _triage(self, incident_id: str, meta: str) -> TriagePlan:
        if not self.use_llm_triage:
            return _default_triage()
        messages = [
            {"role": "system", "content": P.TRIAGE_SYSTEM},
            {"role": "user", "content": P.TRIAGE_USER.format(incident_id=incident_id, meta=meta)},
        ]
        try:
            return generate_structured(
                self.client, messages, TriagePlan, max_repairs=self.max_repairs, temperature=self.temperature
            )
        except (LLMUnavailable, StructuredOutputError) as exc:
            self.transcript.append(f"[triage failed, dispatching all workers with default focus: {type(exc).__name__}]")
            return _default_triage()

    # -- workers --------------------------------------------------------------
    def _run_worker(self, name: str, incident_id: str, meta: str, focus: str, hypotheses: str) -> WorkerOutcome:
        out = WorkerOutcome(name)
        try:
            if name in self.fail_workers:
                raise RuntimeError("injected worker failure")
            tools = WORKER_TOOLS[name]
            toolbox = IncidentToolbox(incident_id, fail_tools=self.fail_tools, allowed_tools=tools)
            system = P.WORKER_BASE.format(name=name, domain=P.WORKER_DOMAINS[name])
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": P.WORKER_USER.format(
                        incident_id=incident_id, meta=meta, focus=focus, hypotheses=hypotheses
                    ),
                },
            ]
            ToolLoop(
                self.client,
                toolbox,
                specs_for(tools),
                transcript=out.transcript,
                unavailable=out.unavailable,
                temperature=self.temperature,
                max_steps=self.worker_max_steps,
                max_tool_calls=self.worker_max_tool_calls,
            ).run(messages)
            joined = "\n\n".join(out.transcript) or "(no tool output)"
            report_msgs = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": P.WORKER_REPORT_USER.format(
                        transcript=joined[-6000:], unavailable=", ".join(sorted(out.unavailable)) or "none"
                    ),
                },
            ]
            out.report = generate_structured(
                self.client, report_msgs, WorkerReport, max_repairs=self.max_repairs, temperature=self.temperature
            )
        except LLMConfigError:
            raise  # wrong key/model: fail loudly, never produce a run of silent zeros
        except Exception as exc:  # a worker must never take the whole incident down
            out.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out

    def _dispatch(self, incident_id: str, meta: str, plan: TriagePlan) -> list[WorkerOutcome]:
        # first assignment per worker wins
        focus: dict[str, str] = {a.worker: a.focus for a in reversed(plan.assignments)}
        hypotheses = "; ".join(plan.hypotheses)
        jobs = [(w, incident_id, meta, focus.get(w, _DEFAULT_FOCUS), hypotheses) for w in WORKER_ORDER]
        if self.parallel:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(self._run_worker, *job) for job in jobs]
                return [f.result() for f in futures]  # deterministic order; LLMConfigError surfaces here
        return [self._run_worker(*job) for job in jobs]

    # -- synthesis ------------------------------------------------------------
    def _reports_json(self, outcomes: list[WorkerOutcome]) -> str:
        return reports_json(outcomes)

    def run(self, incident_id: str) -> Diagnosis:
        self._reset()
        meta = IncidentToolbox(incident_id).call("get_incident_meta", {})
        plan = self._triage(incident_id, meta)
        self.triage = plan
        self.plan_history.append(plan)
        outcomes = self._dispatch(incident_id, meta, plan)
        self.outcomes = {o.name: o for o in outcomes}
        for o in outcomes:
            self.unavailable |= o.unavailable
            self.transcript += [f"[{o.name}] {line}" for line in o.transcript]
            if o.error:
                self.transcript.append(f"[{o.name}] WORKER FAILED: {o.error}")
        failed = {o.name: o.error for o in outcomes if o.report is None}
        joined = "\n\n".join(f"## {o.name} worker\n" + "\n\n".join(o.transcript) for o in outcomes)
        calls = sum(1 for o in outcomes for line in o.transcript if line.startswith("> "))
        if len(failed) == len(outcomes):
            self.degraded = "all workers failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items())
            return degraded_diagnosis(self.agent_type, incident_id, joined, self.degraded, calls)
        messages = [
            {"role": "system", "content": P.SYNTH_SYSTEM},
            {
                "role": "user",
                "content": P.SYNTH_USER.format(
                    incident_id=incident_id,
                    meta=meta,
                    hypotheses="; ".join(plan.hypotheses),
                    reports=self._reports_json(outcomes),
                    failed="; ".join(f"{k} ({v})" for k, v in failed.items()) or "none",
                ),
            },
        ]
        try:
            out = generate_structured(
                self.client, messages, LLMDiagnosis, max_repairs=self.max_repairs, temperature=self.temperature
            )
        except (LLMUnavailable, StructuredOutputError) as exc:
            self.degraded = f"synthesis failed ({type(exc).__name__}): {str(exc)[:200]}"
            return degraded_diagnosis(self.agent_type, incident_id, joined, self.degraded, calls)
        notes = [f"Coverage: worker(s) failed: {', '.join(sorted(failed))}; confidence reflects missing data."]
        d = ground_diagnosis(self.agent_type, incident_id, out, joined, notes=notes if failed else None)
        return apply_risk_guard(d)[0] if self.risk_guard else d
