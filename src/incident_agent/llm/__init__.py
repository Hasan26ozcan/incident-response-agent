"""LLM access layer: client, cache, structured output, scripted test double."""

from incident_agent.llm.cache import CachedClient
from incident_agent.llm.client import (
    FallbackClient,
    LLMClient,
    LLMConfigError,
    LLMResponse,
    LLMUnavailable,
    OpenAICompatClient,
    ToolCall,
    build_client,
)
from incident_agent.llm.scripted import ScriptedClient
from incident_agent.llm.structured import StructuredOutputError, generate_structured
from incident_agent.llm.usage import TrackingClient

__all__ = [
    "CachedClient",
    "FallbackClient",
    "LLMClient",
    "LLMConfigError",
    "LLMResponse",
    "LLMUnavailable",
    "OpenAICompatClient",
    "ScriptedClient",
    "StructuredOutputError",
    "ToolCall",
    "TrackingClient",
    "build_client",
    "generate_structured",
]
