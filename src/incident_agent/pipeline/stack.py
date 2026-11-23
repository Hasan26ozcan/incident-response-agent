"""Compose the LLM client stack from the Stage 21 parts: window -> (cascade of traced clients | traced client)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from incident_agent.llm.client import GROQ_BASE_URL, LLMClient, LLMConfigError, OpenAICompatClient
from incident_agent.observe.cascade import CascadeClient
from incident_agent.observe.llm import TracedClient
from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.trace import Tracer
from incident_agent.observe.window import SlidingWindow, WindowedClient

DEFAULT_CHEAP = "openai/gpt-oss-20b"
DEFAULT_STRONG = "openai/gpt-oss-120b"


@dataclass(frozen=True)
class StackConfig:
    cascade: bool = False  # try CASCADE_CHEAP_MODEL first, escalate to CASCADE_STRONG_MODEL (Groq)
    window_tokens: int | None = None  # sliding-window budget; None = no trimming


def groq_pair(cache_dir: str | None = None) -> tuple[LLMClient, LLMClient]:
    """Cheap and strong Groq clients from the environment. The key never leaves the environment."""
    from incident_agent.llm.cache import CachedClient

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMConfigError("cascade needs GROQ_API_KEY in the environment or .env")
    cheap = os.environ.get("CASCADE_CHEAP_MODEL", DEFAULT_CHEAP)
    strong = os.environ.get("CASCADE_STRONG_MODEL", DEFAULT_STRONG)
    d = cache_dir or os.environ.get("LLM_CACHE_DIR", ".llm_cache")
    return (
        CachedClient(OpenAICompatClient(cheap, key, GROQ_BASE_URL), d),
        CachedClient(OpenAICompatClient(strong, key, GROQ_BASE_URL), d),
    )


def build_stack(
    tracer: Tracer,
    cfg: StackConfig,
    *,
    base: LLMClient | None = None,
    pair: tuple[LLMClient, LLMClient] | None = None,
    prices: PriceTable | None = None,
) -> Any:
    prices = prices or PriceTable.load()
    if cfg.cascade:
        cheap, strong = pair or groq_pair()
        client: Any = CascadeClient(
            TracedClient(cheap, tracer, prices, "cheap"), TracedClient(strong, tracer, prices, "strong"), tracer=tracer
        )
    else:
        if base is None:
            from incident_agent.llm import build_client

            base = build_client()
        client = TracedClient(base, tracer, prices, "llm")
    if cfg.window_tokens:
        client = WindowedClient(client, SlidingWindow(cfg.window_tokens))
    return client
