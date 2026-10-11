"""Shared offline test doubles for the multi-agent stages."""

from __future__ import annotations

import json
import threading
from typing import Any

from pydantic import BaseModel

from incident_agent.agents.llm_commander import CommanderReview
from incident_agent.agents.llm_orchestrator import TriagePlan, WorkerReport
from incident_agent.agents.llm_react_agent import LLMDiagnosis
from incident_agent.llm import LLMResponse, ToolCall

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
REVIEW = {
    "narrative": "At 00:40 CPU and errors rose on checkout-api; the log worker saw an unbounded retry loop.",
    "agrees_with_diagnosis": True,
    "disagreement": "",
    "revised": None,
    "severity": "SEV2",
    "severity_rationale": "Major degradation of the checkout service.",
    "open_questions": ["Was a client retrying aggressively?"],
}


def _json(o: Any) -> LLMResponse:
    return LLMResponse(content=json.dumps(o), usage={"prompt_tokens": 10, "completion_tokens": 5})


class RoutingFake:
    """Thread-safe fake that answers by role (system prompt), so concurrent workers cannot race a script."""

    model = "routing-fake"

    def __init__(
        self,
        *,
        fail: dict[str, Exception] | None = None,
        final: Any = FINAL,
        report: Any = REPORT,
        review: Any = REVIEW,
    ) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.tools_seen: list[tuple[str, bool]] = []  # (role, request carried tools)
        self.fail = fail or {}
        self.final = final
        self.report = report
        self.review = review
        self.tool_turns: dict[str, int] = {}

    @staticmethod
    def role(messages: list[dict[str, Any]]) -> str:
        sys = messages[0]["content"]
        if sys.startswith("You are the triage lead"):
            return "triage"
        if sys.startswith("You are the incident lead"):
            return "synth"
        if sys.startswith("You are the incident commander"):
            return "commander"
        for name in ("log", "metrics", "deploy"):
            if sys.startswith(f"You are the {name} specialist"):
                return name
        return "unknown"

    @staticmethod
    def _body(obj: Any) -> LLMResponse:
        return LLMResponse(content=obj if isinstance(obj, str) else json.dumps(obj))

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        role = self.role(messages)
        with self.lock:
            self.calls.append((role, response_schema.__name__ if response_schema else "tools"))
            self.tools_seen.append((role, bool(tools)))
            turn = self.tool_turns.get(role, 0)
            if tools:
                self.tool_turns[role] = turn + 1
        if role in self.fail:
            raise self.fail[role]
        if response_schema is TriagePlan:
            return _json(TRIAGE)
        if response_schema is LLMDiagnosis:
            return self._body(self.final)
        if response_schema is WorkerReport:
            return self._body(self.report)
        if response_schema is CommanderReview:
            return self._body(self.review)
        if tools and turn == 0:
            name, args = TOOL_FOR[role]
            return LLMResponse(tool_calls=[ToolCall(id=f"{role}0", name=name, arguments=args)])
        return LLMResponse(content="ready")
