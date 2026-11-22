"""Sliding-window context management (Stage 21): keep prompts bounded without breaking tool-call pairing.

Always keeps system messages and the first user message (the task). From the rest it keeps the newest turns that fit a
token budget (``chars/4`` estimate). An assistant message with tool_calls and its tool replies are one unit: they are
kept or dropped together so the provider never sees an orphan tool message. Older dropped turns are replaced by one
short marker so the model knows context was elided.
"""

from __future__ import annotations

from typing import Any


def estimate_tokens(msg: dict[str, Any]) -> int:
    n = len(str(msg.get("content") or ""))
    for tc in msg.get("tool_calls") or []:
        n += len(str(tc))
    return n // 4 + 4


def _units(rest: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    units: list[list[dict[str, Any]]] = []
    for m in rest:
        if m.get("role") == "tool" and units:
            units[-1].append(m)
        else:
            units.append([m])
    return units


class SlidingWindow:
    def __init__(self, max_tokens: int = 6000, marker: bool = True) -> None:
        if max_tokens < 64:
            raise ValueError("max_tokens too small")
        self.max_tokens = max_tokens
        self.marker = marker

    def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        head: list[dict[str, Any]] = []
        i = 0
        while i < len(messages) and messages[i].get("role") == "system":
            head.append(messages[i])
            i += 1
        if i < len(messages) and messages[i].get("role") == "user":
            head.append(messages[i])
            i += 1
        rest = messages[i:]
        budget = self.max_tokens - sum(estimate_tokens(m) for m in head)
        kept: list[list[dict[str, Any]]] = []
        dropped = 0
        units = _units(rest)
        for idx in range(len(units) - 1, -1, -1):
            cost = sum(estimate_tokens(m) for m in units[idx])
            if cost <= budget or not kept:  # always keep the newest unit
                kept.append(units[idx])
                budget -= cost
            else:
                dropped = idx + 1
                break
        kept.reverse()
        out = list(head)
        if dropped and self.marker:
            out.append({"role": "user", "content": f"[context trimmed: {dropped} earlier turn(s) omitted]"})
        for u in kept:
            out.extend(u)
        return out


class WindowedClient:
    """Applies a SlidingWindow before every call."""

    def __init__(self, inner: Any, window: SlidingWindow) -> None:
        self.inner, self.window, self.model = inner, window, inner.model

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self.inner.chat(self.window.apply(messages), **kwargs)
