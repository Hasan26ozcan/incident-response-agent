"""Provider-agnostic LLM client (Stage 3-5 foundation).

One narrow interface (``LLMClient.chat``) so agents never import a vendor SDK.
Groq, Cerebras and Gemini all expose OpenAI-compatible endpoints, so a single
``OpenAICompatClient`` covers them; ``FallbackClient`` rolls over to the next
provider when one is rate-limited, which matters on free tiers.

Note (Groq docs): structured outputs and tool use cannot be combined in one
request. Agents therefore use tools during investigation and a separate
schema-constrained call for the final answer; ``chat`` enforces this.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"


class LLMUnavailable(RuntimeError):
    """Provider unreachable or rate-limited even after retries."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "content": self.content,
                "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in self.tool_calls],
                "usage": self.usage,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> LLMResponse:
        d = json.loads(raw)
        return cls(
            content=d["content"],
            tool_calls=[ToolCall(**t) for t in d["tool_calls"]],
            usage=d.get("usage", {}),
        )


class LLMClient(Protocol):
    model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


class OpenAICompatClient:
    """Chat client for any OpenAI-compatible endpoint (Groq, Cerebras, ...)."""

    def __init__(self, model: str, api_key: str, base_url: str, max_retries: int = 5) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, max_retries=max_retries)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        import openai

        if tools and response_schema:
            raise ValueError("tools and response_schema cannot be combined in one request")
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": response_schema.model_json_schema(),
                    "strict": False,  # best-effort; we validate with Pydantic and repair
                },
            }
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            raise LLMUnavailable(f"{self.model}: {exc}") from exc
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args if isinstance(args, dict) else {}))
        usage = {}
        if resp.usage:
            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        return LLMResponse(content=msg.content, tool_calls=calls, usage=usage)


class FallbackClient:
    """Try each client in order; move on when one is unavailable."""

    def __init__(self, clients: list[LLMClient]) -> None:
        if not clients:
            raise ValueError("FallbackClient needs at least one client")
        self.clients = clients
        self.model = clients[0].model

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        last: Exception | None = None
        for c in self.clients:
            try:
                return c.chat(messages, **kwargs)
            except LLMUnavailable as exc:
                last = exc
        raise LLMUnavailable(f"all providers unavailable: {last}")


def build_client(cache_dir: str | None = None) -> LLMClient:
    """Build the default client from environment variables.

    GROQ_API_KEY (+ optional GROQ_MODEL) is primary. CEREBRAS_API_KEY with
    CEREBRAS_MODEL adds a fallback. Model ids change often on free tiers, so
    verify them in each provider's console before relying on the defaults.
    """
    from incident_agent.llm.cache import CachedClient

    clients: list[LLMClient] = []
    if key := os.environ.get("GROQ_API_KEY"):
        clients.append(OpenAICompatClient(os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), key, GROQ_BASE_URL))
    if (key := os.environ.get("CEREBRAS_API_KEY")) and (model := os.environ.get("CEREBRAS_MODEL")):
        clients.append(OpenAICompatClient(model, key, CEREBRAS_BASE_URL))
    if not clients:
        raise LLMUnavailable("set GROQ_API_KEY (and optionally CEREBRAS_API_KEY + CEREBRAS_MODEL)")
    inner: LLMClient = clients[0] if len(clients) == 1 else FallbackClient(clients)
    return CachedClient(inner, cache_dir or os.environ.get("LLM_CACHE_DIR", ".llm_cache"))
