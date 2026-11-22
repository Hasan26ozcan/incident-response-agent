# ruff: noqa: E501, UP031
"""Cascade routing (Stage 21): try the cheap model first, escalate to the strong one only when the answer is not good enough.

``accept`` is the quality gate: it receives the cheap response and must return True to keep it. Default gate: no error,
non-empty content or tool calls, and (when a schema is requested) the content parses as that schema. Escalation is
also taken when the cheap model is unavailable. Config errors are never swallowed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from incident_agent.llm.client import LLMClient, LLMResponse, LLMUnavailable
from incident_agent.observe.trace import Tracer

Accept = Callable[[LLMResponse, type[BaseModel] | None], bool]


def default_accept(resp: LLMResponse, schema: type[BaseModel] | None) -> bool:
    if schema is not None:
        try:
            schema.model_validate_json(resp.content or "")
        except (ValidationError, ValueError):
            return False
        return True
    return bool((resp.content or "").strip()) or bool(resp.tool_calls)


class CascadeClient:
    def __init__(
        self, cheap: LLMClient, strong: LLMClient, accept: Accept = default_accept, tracer: Tracer | None = None
    ) -> None:
        self.cheap, self.strong, self.accept, self.tracer = cheap, strong, accept, tracer
        self.model = cheap.model
        self.stats = {"cheap_ok": 0, "escalated": 0, "unavailable": 0}

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        reason = "rejected"
        try:
            resp = self.cheap.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
            if self.accept(resp, response_schema):
                self.stats["cheap_ok"] += 1
                self._note("cheap", None)
                return resp
        except LLMUnavailable:
            reason = "unavailable"
            self.stats["unavailable"] += 1
        self.stats["escalated"] += 1
        self._note("strong", reason)
        return self.strong.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)

    def _note(self, tier: str, reason: str | None) -> None:
        if self.tracer:
            self.tracer.event(
                "cascade.route", "llm", tier=tier, reason=reason, cheap=self.cheap.model, strong=self.strong.model
            )

    def escalation_rate(self) -> float:
        n = self.stats["cheap_ok"] + self.stats["escalated"]
        return self.stats["escalated"] / n if n else 0.0
