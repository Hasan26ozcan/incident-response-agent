"""Stage 6 (LLM orchestrator-workers): triage -> concurrent specialist workers -> synthesis. Offline."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from pydantic import BaseModel

from incident_agent.agents.llm_orchestrator import (
    LLMOrchestrator,
    TriagePlan,
    WorkerReport,
)
from incident_agent.agents.llm_react_agent import LLMDiagnosis
from incident_agent.llm import LLMConfigError, LLMResponse, LLMUnavailable, ToolCall, TrackingClient
from incident_agent.prompts import orchestrator_prompts as P
from incident_agent.schemas.agent_output import RiskTier
from incident_agent.tools.toolbox import IncidentToolbox, ToolError

TOOL_FOR = {"log": ("log_overview", {}), "metrics": ("get_metric_summary", {}), "deploy": ("get_recent_deploys", {})}
FINAL = {
    "is_false_alarm": False,
    "root_cause": "An unbounded retry loop saturated the checkout service CPU.",
    "confidence": 0.8,
    "evidence": [{"source_type": "log_entry", "source": "logs.log", "detail": "retry loop", "timestamp": ""}],
    "affected_service": "checkout-api",
    "category": "cpu_exhaustion",
    "reasoning_steps": ["CPU rose first", "no deploy changes"],
    "recommendation": "Restart the pods and cap the retry loop.",
    "risk_tier": "high",
}
REPORT = {
    "domain_normal": False,
    "summary": "Errors dominate this domain after the onset.",
    "facts": [{"claim": "errors rise", "quote": "ERROR", "timestamp": ""}],
    "anomaly_onset": "00:40",
    "suspected_cause": "retry loop",
    "confidence": 0.7,
}
TRIAGE = {
    "hypotheses": ["retry storm"],
    "assignments": [{"worker": "log", "focus": "find first errors"}, {"worker": "metrics", "focus": "cpu onset"}],
}


def _json(o: Any) -> LLMResponse:
    return LLMResponse(content=json.dumps(o), usage={"prompt_tokens": 10, "completion_tokens": 5})


class RoutingFake:
    """Thread-safe fake that answers by role (system prompt), so concurrent workers cannot race a script."""

    model = "routing-fake"

    def __init__(self, *, fail: dict[str, Exception] | None = None, final: Any = FINAL, report: Any = REPORT) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.fail = fail or {}
        self.final = final
        self.report = report
        self.tool_turns: dict[str, int] = {}

    @staticmethod
    def role(messages: list[dict[str, Any]], schema: type[BaseModel] | None) -> str:
        sys = messages[0]["content"]
        if sys.startswith("You are the triage lead"):
            return "triage"
        if sys.startswith("You are the incident lead"):
            return "synth"
        for name in ("log", "metrics", "deploy"):
            if sys.startswith(f"You are the {name} specialist"):
                return name
        return "unknown"

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        role = self.role(messages, response_schema)
        with self.lock:
            self.calls.append((role, response_schema.__name__ if response_schema else "tools"))
            turn = self.tool_turns.get(role, 0)
            if tools:
                self.tool_turns[role] = turn + 1
        if role in self.fail:
            raise self.fail[role]
        if response_schema is TriagePlan:
            return _json(TRIAGE)
        if response_schema is LLMDiagnosis:
            return (
                LLMResponse(content=json.dumps(self.final))
                if not isinstance(self.final, str)
                else LLMResponse(content=self.final)
            )
        if response_schema is WorkerReport:
            return LLMResponse(content=json.dumps(self.report))
        if tools and turn == 0:
            name, args = TOOL_FOR[role]
            return LLMResponse(tool_calls=[ToolCall(id=f"{role}0", name=name, arguments=args)])
        return LLMResponse(content="ready")


def test_full_pipeline_dispatches_three_workers_and_synthesises():
    fake = RoutingFake()
    agent = LLMOrchestrator(fake)
    d = agent.run("INC-001")
    assert d.agent_type == "llm_orchestrator" and d.risk_tier is RiskTier.HIGH and agent.degraded is None
    assert set(agent.outcomes) == {"log", "metrics", "deploy"} and all(o.report for o in agent.outcomes.values())
    roles = [r for r, _ in fake.calls]
    assert roles.count("triage") == 1 and roles.count("synth") == 1
    assert {"log", "metrics", "deploy"} <= set(roles)


def test_each_worker_only_sees_its_own_tools():
    seen: dict[str, set[str]] = {}

    class Spy(RoutingFake):
        def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
            if tools:
                seen.setdefault(self.role(messages, response_schema), set()).update(
                    t["function"]["name"] for t in tools
                )
            return super().chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)

    LLMOrchestrator(Spy()).run("INC-001")
    assert seen["log"] == {"log_overview", "search_logs"}
    assert seen["metrics"] == {"get_metric_summary"} and seen["deploy"] == {"get_recent_deploys"}


def test_toolbox_enforces_worker_scope_even_if_model_ignores_the_spec():
    tb = IncidentToolbox("INC-001", allowed_tools={"get_metric_summary"})
    assert tb.call("get_metric_summary", {})
    with pytest.raises(ToolError) as exc:
        tb.call("log_overview", {})
    assert exc.value.bad_args and "not available" in str(exc.value)


def test_sequential_and_parallel_modes_agree():
    a = LLMOrchestrator(RoutingFake(), parallel=True).run("INC-001")
    b = LLMOrchestrator(RoutingFake(), parallel=False).run("INC-001")
    assert a.root_cause == b.root_cause and a.confidence == b.confidence


def test_one_failed_worker_does_not_lose_the_incident():
    agent = LLMOrchestrator(RoutingFake(), fail_workers={"metrics"})
    d = agent.run("INC-001")
    assert agent.degraded is None and agent.outcomes["metrics"].report is None
    assert "RuntimeError" in agent.outcomes["metrics"].error
    assert any("worker(s) failed: metrics" in s.description for s in d.reasoning_steps)


def test_provider_outage_in_one_worker_is_isolated():
    agent = LLMOrchestrator(RoutingFake(fail={"deploy": LLMUnavailable("down")}))
    d = agent.run("INC-001")
    assert agent.outcomes["deploy"].error.startswith("LLMUnavailable") and d.root_cause


def test_all_workers_failing_degrades_instead_of_crashing():
    agent = LLMOrchestrator(RoutingFake(), fail_workers={"log", "metrics", "deploy"})
    d = agent.run("INC-001")
    assert agent.degraded and "all workers failed" in agent.degraded
    assert d.confidence == 0.0 and d.risk_tier is RiskTier.HIGH and d.root_cause.lower().startswith("unknown")


def test_synthesis_failure_degrades():
    agent = LLMOrchestrator(RoutingFake(fail={"synth": LLMUnavailable("down")}))
    d = agent.run("INC-001")
    assert "synthesis failed" in (agent.degraded or "") and d.confidence == 0.0


def test_garbage_synthesis_output_degrades():
    agent = LLMOrchestrator(RoutingFake(final="not json"))
    agent.run("INC-001")
    assert "StructuredOutputError" in (agent.degraded or "")


def test_triage_failure_falls_back_to_default_focus_for_all_workers():
    agent = LLMOrchestrator(RoutingFake(fail={"triage": LLMUnavailable("down")}))
    d = agent.run("INC-001")
    assert agent.degraded is None and d.root_cause and len(agent.outcomes) == 3
    assert any("triage failed" in line for line in agent.transcript)


def test_triage_can_be_disabled_for_ablation():
    fake = RoutingFake()
    LLMOrchestrator(fake, use_llm_triage=False).run("INC-001")
    assert "triage" not in [r for r, _ in fake.calls]


def test_config_errors_are_not_swallowed():
    with pytest.raises(LLMConfigError):
        LLMOrchestrator(RoutingFake(fail={"log": LLMConfigError("bad key")})).run("INC-001")


def test_triage_cannot_remove_workers():
    # TRIAGE above only assigns log+metrics; deploy must still run with the default focus
    agent = LLMOrchestrator(RoutingFake())
    agent.run("INC-001")
    assert agent.outcomes["deploy"].report is not None


def test_ungrounded_evidence_is_penalised_across_worker_transcripts():
    fake_ev = {"source_type": "log_entry", "source": "x", "detail": "kernel panic node-99 zzqx", "timestamp": ""}
    d = LLMOrchestrator(RoutingFake(final={**FINAL, "evidence": [fake_ev]})).run("INC-001")
    assert d.confidence < 0.8 and d.evidence[0].confidence_weight < 1.0


def test_false_alarm_marking():
    d = LLMOrchestrator(RoutingFake(final={**FINAL, "is_false_alarm": True, "risk_tier": "low"})).run("INC-019")
    assert d.root_cause.lower().startswith("false alarm") and d.risk_tier is RiskTier.LOW


def test_tracking_client_is_thread_safe_under_parallel_workers():
    class Metered(RoutingFake):
        def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
            r = super().chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
            r.usage = {"prompt_tokens": 1, "completion_tokens": 2}
            return r

    for _ in range(25):  # repeat: a data race would show up as lost increments
        tracked = TrackingClient(Metered())
        LLMOrchestrator(tracked).run("INC-001")
        assert tracked.calls == len(tracked.inner.calls)  # type: ignore[attr-defined]
        assert tracked.prompt_tokens == tracked.calls and tracked.completion_tokens == 2 * tracked.calls


def test_synthesis_prompt_formats_and_marks_verified_facts():
    text = P.SYNTH_USER.format(incident_id="INC-001", meta="{}", hypotheses="h", reports="{}", failed="none")
    assert "Produce the final diagnosis as JSON." in text and '"is_false_alarm": false' in text


def test_reports_mark_unverifiable_quotes():
    agent = LLMOrchestrator(
        RoutingFake(report={**REPORT, "facts": [{"claim": "made up", "quote": "zzqx node-99 panic", "timestamp": ""}]})
    )
    agent.run("INC-001")
    blob = json.loads(agent._reports_json(list(agent.outcomes.values())))
    assert all(f["verified"] is False for f in blob["log"]["facts"])


def test_harness_adapter_is_registered_and_scores(monkeypatch):
    from incident_agent.eval import harness
    from incident_agent.llm import client as client_mod

    assert "llm_orchestrator" in harness.ADAPTER_NAMES
    monkeypatch.setattr(client_mod, "build_client", lambda *a, **k: RoutingFake())
    monkeypatch.setattr("incident_agent.llm.build_client", lambda *a, **k: RoutingFake())
    run = harness.make_adapter("llm_orchestrator")
    res = run("INC-001")
    assert res.risk_tier == "high" and "workers" in res.trace and res.usage["calls"] > 0
    rep = harness.evaluate(run, ids=["INC-001"])
    assert rep["summary"]["crashed"] == 0
