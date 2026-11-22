# ruff: noqa: E501, UP031
"""Prompt-cache friendliness (Stage 21). Providers (Groq gpt-oss: automatic, identical prefix, ~128-1024 token minimum)
only reuse the *longest identical prefix*. This measures how much of each prompt is a repeat of the previous one, and
flags the usual cache-breakers: volatile text (timestamps/uuids) early in the prompt, and tool lists that change."""

from __future__ import annotations

import re
from typing import Any

_VOLATILE = re.compile(
    r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b\d{10}(\.\d+)?\b"
)
MIN_CACHEABLE_TOKENS = 1024  # conservative: Groq documents 128-1024 depending on model


def _flat(messages: list[dict[str, Any]]) -> str:
    return "\n".join(f"{m.get('role')}:{m.get('content') or ''}" for m in messages)


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def analyze(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """calls: [{"messages": [...], "tools": [...]|None}] in order. Returns reuse ratios and warnings."""
    reuse, warnings = [], []
    prev: str | None = None
    prev_tools: Any = None
    for i, c in enumerate(calls):
        text = _flat(c["messages"])
        if prev is not None:
            reuse.append(common_prefix_len(prev, text) / max(len(text), 1))
            if c.get("tools") != prev_tools:
                warnings.append(f"call {i}: tools changed -> provider cache invalidated")
        m = _VOLATILE.search(text)
        if m and m.start() < len(text) // 2:
            warnings.append(
                f"call {i}: volatile value (timestamp/uuid) at char {m.start()} sits in the cacheable prefix"
            )
        prev, prev_tools = text, c.get("tools")
    mean = sum(reuse) / len(reuse) if reuse else 0.0
    est_tokens = (len(prev) // 4) if prev else 0
    if prev is not None and est_tokens < MIN_CACHEABLE_TOKENS:
        warnings.append(
            f"prompts are ~{est_tokens} tokens: below the provider's minimum cacheable size, caching will not trigger"
        )
    return {"calls": len(calls), "mean_prefix_reuse": round(mean, 4), "warnings": warnings}
