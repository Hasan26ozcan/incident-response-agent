"""Model specs: ``groq/openai/gpt-oss-120b``, ``mistral/mistral-large-latest``, ``cerebras/<model>``, and
``cascade:<cheap spec>><strong spec>``. Keys come from the environment only (``<PROVIDER>_API_KEY``)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from incident_agent.llm.client import (
    CEREBRAS_BASE_URL,
    GROQ_BASE_URL,
    MISTRAL_BASE_URL,
    LLMClient,
    LLMConfigError,
    OpenAICompatClient,
)
from incident_agent.observe.cascade import CascadeClient
from incident_agent.observe.llm import TracedClient
from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.trace import Tracer

BASE_URLS = {"groq": GROQ_BASE_URL, "cerebras": CEREBRAS_BASE_URL, "mistral": MISTRAL_BASE_URL}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


def parse(text: str) -> ModelSpec:
    provider, _, model = text.partition("/")
    if provider not in BASE_URLS or not model:
        raise ValueError(f"bad model spec {text!r}: use <provider>/<model> with provider in {sorted(BASE_URLS)}")
    return ModelSpec(provider, model)


def is_cascade(text: str) -> bool:
    return text.startswith("cascade:")


def parse_cascade(text: str) -> tuple[ModelSpec, ModelSpec]:
    body = text.removeprefix("cascade:")
    cheap, sep, strong = body.partition(">")
    if not sep:
        raise ValueError(f"bad cascade spec {text!r}: use cascade:<cheap>><strong>")
    return parse(cheap), parse(strong)


def required_keys(text: str) -> list[str]:
    specs = parse_cascade(text) if is_cascade(text) else (parse(text),)
    return sorted({f"{s.provider.upper()}_API_KEY" for s in specs})


def build_one(spec: ModelSpec) -> LLMClient:
    key = os.environ.get(f"{spec.provider.upper()}_API_KEY")
    if not key:
        raise LLMConfigError(f"{spec.provider.upper()}_API_KEY is not set (put it in your local .env, never in chat)")
    if spec.provider == "mistral":  # free tier is rate limited: pace the calls
        interval = float(os.environ.get("MISTRAL_MIN_INTERVAL", "3.0"))
        return OpenAICompatClient(spec.model, key, BASE_URLS["mistral"], min_interval=interval)
    return OpenAICompatClient(spec.model, key, BASE_URLS[spec.provider])


def build_client(text: str, tracer: Tracer, prices: PriceTable | None = None) -> LLMClient:
    """A traced (and, for cascade specs, routed) client. No disk cache: measurements must hit the real provider."""
    prices = prices or PriceTable.load()
    if is_cascade(text):
        cheap, strong = parse_cascade(text)
        return CascadeClient(
            TracedClient(build_one(cheap), tracer, prices, "cheap"),
            TracedClient(build_one(strong), tracer, prices, "strong"),
            tracer=tracer,
        )
    return TracedClient(build_one(parse(text)), tracer, prices, "llm")
