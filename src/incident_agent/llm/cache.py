"""Disk cache for LLM calls: reproducible evals and spared free-tier quota."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from incident_agent.llm.client import LLMClient, LLMResponse


class CachedClient:
    def __init__(self, inner: LLMClient, cache_dir: str | Path) -> None:
        self.inner = inner
        self.model = inner.model
        self.dir = Path(cache_dir)
        self.hits = 0
        self.misses = 0

    def _key(self, messages: list[dict[str, Any]], tools: Any, schema: type[BaseModel] | None, temp: float) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "schema": schema.model_json_schema() if schema else None,
            "temperature": temp,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        path = self.dir / f"{self._key(messages, tools, response_schema, temperature)}.json"
        if path.exists():
            self.hits += 1
            return LLMResponse.from_json(path.read_text())
        self.misses += 1
        resp = self.inner.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.to_json())
        return resp
