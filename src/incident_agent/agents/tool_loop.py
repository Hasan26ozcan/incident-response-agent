"""Reusable guarded tool-use loop (shared by the single ReAct agent and the specialist workers).

Guards: identical calls are not re-executed, total tool calls are budgeted, malformed calls are
bounded, tool output is wrapped as untrusted data, and every tool-call id always gets an answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from incident_agent.llm import LLMClient
from incident_agent.tools.toolbox import TOOL_NAMES, IncidentToolbox, ToolError


def wrap_output(text: str) -> str:
    """Mark tool output as untrusted data; neutralise a forged closing tag inside it."""
    return "<tool_output>\n" + text.replace("</tool_output>", "[/tool_output]") + "\n</tool_output>"


class ToolLoop:
    """Runs ``chat -> tool calls -> results`` until the model stops calling tools or a guard trips.

    ``transcript`` and ``unavailable`` are injected mutable containers so the owner can read them
    even if the loop is interrupted by an exception.
    """

    def __init__(
        self,
        client: LLMClient,
        toolbox: IncidentToolbox,
        specs: list[dict[str, Any]],
        *,
        transcript: list[str],
        unavailable: set[str],
        temperature: float = 0.0,
        max_steps: int = 8,
        max_tool_calls: int = 12,
        max_bad_calls: int = 4,
        max_duplicates: int = 3,
        on_failure: Callable[[ToolError], str | None] | None = None,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.specs = specs
        self.transcript = transcript
        self.unavailable = unavailable
        self.temperature = temperature
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_bad_calls = max_bad_calls
        self.max_duplicates = max_duplicates
        self.on_failure = on_failure  # returns extra text for the model (e.g. a replan) or None
        self.executed = 0

    def run(self, messages: list[dict[str, Any]]) -> None:
        bad_calls = duplicates = 0
        seen: set[str] = set()
        for _ in range(self.max_steps):
            tools = [t for t in self.specs if t["function"]["name"] not in self.unavailable]
            resp = self.client.chat(messages, tools=tools, temperature=self.temperature)
            if not resp.tool_calls:
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in resp.tool_calls
                    ],
                }
            )
            stop: str | None = None
            for call in resp.tool_calls:  # every call id must get an answer to keep the protocol valid
                key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
                if stop is not None:
                    result = f"SKIPPED: investigation is being stopped ({stop}). Conclude with what you have."
                elif self.executed >= self.max_tool_calls:
                    stop = "tool-call budget exhausted"
                    result = "BUDGET: tool-call budget exhausted. Conclude with the evidence you have."
                elif key in seen:
                    duplicates += 1
                    result = (
                        "DUPLICATE: this exact call was already made (result above). Try something new, or conclude."
                    )
                    if duplicates >= self.max_duplicates:
                        stop = "repeated identical calls"
                else:
                    seen.add(key)
                    self.executed += 1
                    try:
                        result = wrap_output(self.toolbox.call(call.name, call.arguments))
                    except ToolError as failure:
                        result = f"ERROR {failure}"
                        if failure.bad_args:  # the model's mistake: let it correct the call, keep the tool
                            bad_calls += 1
                            if bad_calls >= self.max_bad_calls:
                                stop = "too many malformed tool calls"
                        elif failure.tool in TOOL_NAMES:
                            extra = self.on_failure(failure) if self.on_failure else None
                            self.unavailable.add(failure.tool)  # withdrawn either way
                            if extra:
                                result += f"\n{extra}"
                self.transcript.append(f"> {call.name}({json.dumps(call.arguments)})\n{result}")
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if stop is not None:
                self.transcript.append(f"[investigation stopped: {stop}]")
                return
