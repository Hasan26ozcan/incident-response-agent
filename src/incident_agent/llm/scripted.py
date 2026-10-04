"""Deterministic fake LLM for unit tests (no network, no quota)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from incident_agent.llm.client import LLMResponse


class ScriptedClient:
    """Returns pre-baked responses in order and records every call."""

    def __init__(self, responses: list[LLMResponse], model: str = "scripted") -> None:
        self.model = model
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "schema": response_schema})
        if not self._responses:
            raise AssertionError("ScriptedClient exhausted: agent made more LLM calls than scripted")
        return self._responses.pop(0)
