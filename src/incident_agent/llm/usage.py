"""Per-run usage accounting (calls and tokens) as a transparent client wrapper."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from incident_agent.llm.client import LLMClient, LLMResponse


class TrackingClient:
    """Counts calls and tokens. Cached responses carry their original usage, so totals show true cost."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.model = inner.model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        resp = self.inner.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
        self.calls += 1
        self.prompt_tokens += resp.usage.get("prompt_tokens", 0)
        self.completion_tokens += resp.usage.get("completion_tokens", 0)
        return resp

    def summary(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
