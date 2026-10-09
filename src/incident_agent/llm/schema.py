"""JSON-schema helpers for providers with strict or limited schema support."""

from __future__ import annotations

import copy
from typing import Any

_DROP_KEYS = {"title", "$defs", "definitions"}


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline every ``$ref`` and drop ``$defs`` / ``title``.

    Pydantic emits ``$defs`` + ``$ref`` for nested models. Some providers (notably Google-backed
    ones behind OpenRouter) reject those; a self-contained schema is accepted everywhere.
    """
    defs = {**schema.get("definitions", {}), **schema.get("$defs", {})}

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name in seen or name not in defs:  # recursive or dangling: keep it loose, never loop
                return {"type": "object"}
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return resolve(merged, (*seen, name))
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _DROP_KEYS:
                continue
            if k == "properties" and isinstance(v, dict):  # keys here are field names: never drop them
                out[k] = {name: resolve(sub, seen) for name, sub in v.items()}
            else:
                out[k] = resolve(v, seen)
        return out

    flat: dict[str, Any] = resolve({k: v for k, v in schema.items() if k not in ("$defs", "definitions")}, ())
    return flat
