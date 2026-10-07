"""Schema-validated generation with a repair loop (Stage 4)."""

from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from incident_agent.llm.client import LLMClient

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """The model never produced schema-valid JSON within the repair budget."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1) if m else text


def generate_structured(
    client: LLMClient,
    messages: list[dict[str, Any]],
    schema: type[T],
    *,
    max_repairs: int = 2,
    temperature: float = 0.0,
) -> T:
    """Call the model with a JSON schema; on invalid output, feed the error back and retry."""
    msgs = list(messages)
    last_error = ""
    for _ in range(max_repairs + 1):
        resp = client.chat(msgs, response_schema=schema, temperature=temperature)
        raw = resp.content or ""
        try:
            return schema.model_validate_json(_strip_fences(raw))
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:600]
            msgs = [
                *msgs,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"That output failed validation:\n{last_error}\nReturn ONLY corrected JSON.",
                },
            ]
    raise StructuredOutputError(f"{schema.__name__}: invalid after {max_repairs} repairs: {last_error}")
