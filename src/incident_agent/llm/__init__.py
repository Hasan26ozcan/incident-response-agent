"""LLM access layer: client, cache, structured output, scripted test double."""

from incident_agent.llm.cache import CachedClient
from incident_agent.llm.client import (
    FallbackClient,
    LLMClient,
    LLMResponse,
    LLMUnavailable,
    OpenAICompatClient,
    ToolCall,
    build_client,
)
from incident_agent.llm.scripted import ScriptedClient
from incident_agent.llm.structured import StructuredOutputError, generate_structured

__all__ = [
    "CachedClient",
    "FallbackClient",
    "LLMClient",
    "LLMResponse",
    "LLMUnavailable",
    "OpenAICompatClient",
    "ScriptedClient",
    "StructuredOutputError",
    "ToolCall",
    "build_client",
    "generate_structured",
]
