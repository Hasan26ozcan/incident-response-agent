"""LLM-driven ReAct agent: Stage 3 (loop), Stage 4 (validated output), Stage 5 (plan + replan).

Flow: LLM writes a structured plan -> tool-use loop -> if a tool fails, the failed tool is withdrawn
and the LLM replans from the failure feedback -> a separate schema-constrained call produces the
final diagnosis (Groq cannot combine tools with structured outputs) -> Pydantic validation ->
evidence grounding check against what the tools actually returned.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from incident_agent.agents.grounding import degraded_diagnosis, ground_diagnosis
from incident_agent.agents.risk_guard import apply_risk_guard
from incident_agent.agents.tool_loop import ToolLoop, wrap_output
from incident_agent.llm import LLMClient, LLMUnavailable, StructuredOutputError, generate_structured
from incident_agent.prompts import agent_prompts as P
from incident_agent.schemas.agent_output import EvidenceType, RiskTier
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools.toolbox import TOOL_NAMES, TOOL_SPECS, IncidentToolbox, ToolError


class PlanStepSpec(BaseModel):
    tool: str
    purpose: str


class LLMPlan(BaseModel):
    hypotheses: list[str] = Field(min_length=1, max_length=4)
    steps: list[PlanStepSpec] = Field(min_length=1, max_length=8)


class LLMEvidence(BaseModel):
    source_type: EvidenceType
    source: str = Field(min_length=1)
    detail: str = Field(min_length=3)
    timestamp: str = ""


class LLMDiagnosis(BaseModel):
    is_false_alarm: bool
    root_cause: str = Field(min_length=10)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[LLMEvidence] = Field(min_length=1)
    affected_service: str = Field(min_length=1)
    category: str = Field(min_length=1)
    reasoning_steps: list[str] = Field(min_length=1)
    recommendation: str = Field(min_length=10)
    risk_tier: RiskTier


_wrap_output = wrap_output  # re-exported for tests/back-compat


def _default_plan() -> LLMPlan:
    return LLMPlan(
        hypotheses=["unknown: sweep all read-only sources"],
        steps=[PlanStepSpec(tool=t, purpose="default sweep (planner unavailable)") for t in TOOL_NAMES],
    )


class LLMReActAgent:
    agent_type = "llm_react"

    def __init__(
        self,
        client: LLMClient,
        *,
        max_steps: int = 8,
        max_replans: int = 2,
        max_repairs: int = 2,
        fail_tools: set[str] | None = None,
        temperature: float = 0.0,
        max_tool_calls: int = 12,
        max_bad_calls: int = 4,
        max_duplicates: int = 3,
        risk_guard: bool = False,
        toolbox_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.client = client
        self.toolbox_factory = toolbox_factory  # default IncidentToolbox; e.g. McpToolbox for the MCP servers
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.max_repairs = max_repairs
        self.fail_tools = fail_tools or set()
        self.temperature = temperature
        self.max_tool_calls = max_tool_calls
        self.max_bad_calls = max_bad_calls
        self.max_duplicates = max_duplicates
        self.risk_guard = risk_guard
        self.degraded: str | None = None  # set when the run could not finish normally
        self.plan_history: list[LLMPlan] = []
        self.unavailable: set[str] = set()
        self.transcript: list[str] = []

    # -- planning (Stage 5) -------------------------------------------------
    def _plan(self, incident_id: str, extra_user: str | None = None) -> LLMPlan:
        available = [t for t in TOOL_NAMES if t not in self.unavailable]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": P.PLAN_SYSTEM.format(tools=", ".join(available))},
            {"role": "user", "content": extra_user or f"Plan the investigation of incident {incident_id}."},
        ]
        plan = generate_structured(
            self.client, messages, LLMPlan, max_repairs=self.max_repairs, temperature=self.temperature
        )
        plan.steps = [s for s in plan.steps if s.tool in available] or plan.steps[:1]
        self.plan_history.append(plan)
        return plan

    def _replan(self, incident_id: str, failure: ToolError) -> LLMPlan:
        self.unavailable.add(failure.tool)
        available = [t for t in TOOL_NAMES if t not in self.unavailable]
        prev = self.plan_history[-1].model_dump_json() if self.plan_history else "none"
        text = P.REPLAN_USER.format(tool=failure.tool, reason=failure.reason, available=", ".join(available), plan=prev)
        return self._plan(incident_id, extra_user=f"Incident {incident_id}.\n{text}")

    # -- main loop (Stage 3) --------------------------------------------------
    def run(self, incident_id: str) -> Diagnosis:
        toolbox = (self.toolbox_factory or IncidentToolbox)(incident_id, fail_tools=self.fail_tools)
        self.plan_history, self.unavailable, self.transcript, self.degraded = [], set(), [], None
        try:
            plan = self._plan(incident_id)
        except (LLMUnavailable, StructuredOutputError) as exc:
            self.transcript.append(f"[planner failed, using default plan: {type(exc).__name__}]")
            plan = _default_plan()
            self.plan_history.append(plan)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": P.SYSTEM},
            {
                "role": "user",
                "content": P.INVESTIGATE_USER.format(incident_id=incident_id, plan=plan.model_dump_json()),
            },
        ]
        replans = 0

        def on_failure(failure: ToolError) -> str | None:
            nonlocal replans
            if replans >= self.max_replans:
                return None
            replans += 1
            return f"REPLAN: {self._replan(incident_id, failure).model_dump_json()}"  # withdraws the failed tool

        loop = ToolLoop(
            self.client,
            toolbox,
            TOOL_SPECS,
            transcript=self.transcript,
            unavailable=self.unavailable,
            temperature=self.temperature,
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_bad_calls=self.max_bad_calls,
            max_duplicates=self.max_duplicates,
            on_failure=on_failure,
        )
        try:
            loop.run(messages)
        except LLMUnavailable as exc:
            self.degraded = f"investigation interrupted: {exc}"
            self.transcript.append("[investigation interrupted: provider unavailable]")
        finally:
            close = getattr(toolbox, "close", None)
            if callable(close):
                close()
        return self._finalize(incident_id)

    # -- final structured answer (Stage 4) -----------------------------------
    def _finalize(self, incident_id: str) -> Diagnosis:
        joined = "\n\n".join(self.transcript) or "(no tool output)"
        messages = [
            {"role": "system", "content": P.SYSTEM},
            {
                "role": "user",
                "content": P.FINAL_USER.format(
                    incident_id=incident_id,
                    transcript=joined[-9000:],
                    unavailable=", ".join(sorted(self.unavailable)) or "none",
                ),
            },
        ]
        try:
            out = generate_structured(
                self.client, messages, LLMDiagnosis, max_repairs=self.max_repairs, temperature=self.temperature
            )
        except (LLMUnavailable, StructuredOutputError) as exc:
            reason = f"final diagnosis failed ({type(exc).__name__}): {str(exc)[:200]}"
            self.degraded = f"{self.degraded}; {reason}" if self.degraded else reason
            calls = sum(1 for line in self.transcript if line.startswith("> "))
            return degraded_diagnosis(self.agent_type, incident_id, joined, self.degraded, calls)
        d = ground_diagnosis(self.agent_type, incident_id, out, joined)
        return apply_risk_guard(d)[0] if self.risk_guard else d
