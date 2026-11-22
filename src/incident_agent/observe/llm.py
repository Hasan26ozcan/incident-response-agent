"""TracedClient (Stage 21): wraps any LLMClient and records one ``llm`` span per call. Behaviour is unchanged."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from incident_agent.llm.client import LLMClient, LLMResponse
from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.trace import Tracer


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def prefix_hash(messages: list[dict[str, Any]], tools: Any = None) -> str:
    """Hash of everything except the last message: what a provider prompt cache can reuse."""
    return _digest([messages[:-1], tools])


class TracedClient:
    def __init__(self, inner: LLMClient, tracer: Tracer, prices: PriceTable | None = None, role: str = "llm") -> None:
        self.inner = inner
        self.model = inner.model
        self.tracer = tracer
        self.prices = prices or PriceTable()
        self.role = role

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        with self.tracer.span(
            f"llm.{self.role}",
            "llm",
            model=self.inner.model,
            messages=len(messages),
            prompt_chars=chars,
            tools=len(tools or []),
            structured=response_schema is not None,
            prefix_hash=prefix_hash(messages, tools),
        ) as sp:
            if self.tracer.capture_content:
                sp.attrs["input"] = messages
            resp = self.inner.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
            pt, ct = int(resp.usage.get("prompt_tokens", 0)), int(resp.usage.get("completion_tokens", 0))
            cc = int(resp.usage.get("cached_tokens", 0))
            sp.attrs.update(
                prompt_tokens=pt,
                completion_tokens=ct,
                cached_tokens=cc,
                tool_calls=len(resp.tool_calls),
                cost_usd=self.prices.cost(self.inner.model, pt, ct, cc),
            )
            if self.tracer.capture_content:
                sp.attrs["output"] = resp.content
            return resp
