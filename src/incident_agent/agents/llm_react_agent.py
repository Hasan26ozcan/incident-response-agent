"""LLM-driven ReAct agent: Stage 3 (loop), Stage 4 (validated output), Stage 5 (plan + replan).

Flow: LLM writes a structured plan -> tool-use loop -> if a tool fails, the failed tool is withdrawn
and the LLM replans from the failure feedback -> a separate schema-constrained call produces the
final diagnosis (Groq cannot combine tools with structured outputs) -> Pydantic validation ->
evidence grounding check against what the tools actually returned.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from incident_agent.llm import LLMClient, generate_structured
from incident_agent.prompts import agent_prompts as P
from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep, RiskTier
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


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_.\-]{3,}", text.lower()))


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
    ) -> None:
        self.client = client
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.max_repairs = max_repairs
        self.fail_tools = fail_tools or set()
        self.temperature = temperature
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
        toolbox = IncidentToolbox(incident_id, fail_tools=self.fail_tools)
        self.plan_history, self.unavailable, self.transcript = [], set(), []
        plan = self._plan(incident_id)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": P.SYSTEM},
            {
                "role": "user",
                "content": P.INVESTIGATE_USER.format(incident_id=incident_id, plan=plan.model_dump_json()),
            },
        ]
        replans = 0
        for _ in range(self.max_steps):
            tools = [t for t in TOOL_SPECS if t["function"]["name"] not in self.unavailable]
            resp = self.client.chat(messages, tools=tools, temperature=self.temperature)
            if not resp.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in resp.tool_calls
                    ],
                }
            )
            for call in resp.tool_calls:
                try:
                    result = toolbox.call(call.name, call.arguments)
                except ToolError as failure:
                    result = f"ERROR {failure}"
                    if failure.tool in TOOL_NAMES:
                        if replans < self.max_replans:
                            replans += 1
                            plan = self._replan(incident_id, failure)  # withdraws the failed tool
                            result += f"\nREPLAN: {plan.model_dump_json()}"
                        else:
                            self.unavailable.add(failure.tool)
                self.transcript.append(f"> {call.name}({json.dumps(call.arguments)})\n{result}")
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
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
        out = generate_structured(
            self.client, messages, LLMDiagnosis, max_repairs=self.max_repairs, temperature=self.temperature
        )
        return self._to_diagnosis(incident_id, out, joined)

    def _to_diagnosis(self, incident_id: str, out: LLMDiagnosis, transcript: str) -> Diagnosis:
        seen = _tokens(transcript)
        evidence: list[EvidenceItem] = []
        ungrounded = 0
        for e in out.evidence:
            toks = _tokens(e.detail)
            grounded = bool(toks) and len(toks & seen) / len(toks) >= 0.5
            ungrounded += not grounded
            evidence.append(
                EvidenceItem(
                    source_type=e.source_type,
                    source=e.source,
                    detail=e.detail,
                    timestamp=e.timestamp,
                    confidence_weight=1.0 if grounded else 0.3,
                )
            )
        steps = [ReasoningStep(step_number=i, description=s) for i, s in enumerate(out.reasoning_steps, 1)]
        confidence = out.confidence
        if ungrounded:
            confidence = round(max(0.0, confidence - 0.15 * ungrounded), 2)
            steps.append(
                ReasoningStep(
                    step_number=len(steps) + 1,
                    description=f"Grounding check: {ungrounded} evidence item(s) not found in tool output; "
                    "confidence reduced.",
                )
            )
        root = out.root_cause
        if out.is_false_alarm and "false alarm" not in root.lower():
            root = f"False alarm: no real incident. {root}"
        return Diagnosis(
            agent_type=self.agent_type,
            incident_id=incident_id,
            root_cause=root,
            confidence=confidence,
            evidence=evidence,
            affected_service=out.affected_service,
            category=out.category,
            reasoning_steps=steps,
            recommendation=out.recommendation,
            risk_tier=out.risk_tier,
        )
