# ruff: noqa: E501, UP031
"""Cost model (Stage 21). Prices come from ``pricing.json`` (verified only); unknown models cost ``None``, never a guess."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRICING_FILE = Path(__file__).with_name("pricing.json")


@dataclass(frozen=True)
class Price:
    input: float  # USD per 1M prompt tokens
    output: float
    cached_input: float | None = None  # None -> cached tokens billed at the input rate


class PriceTable:
    def __init__(self, models: dict[str, Price] | None = None) -> None:
        self.models = dict(models or {})

    @classmethod
    def load(cls, path: Path | str | None = None) -> PriceTable:
        raw = json.loads(Path(path or PRICING_FILE).read_text(encoding="utf-8"))
        out: dict[str, Price] = {}
        for name, row in (raw.get("models") or {}).items():
            out[name] = Price(
                float(row["input"]),
                float(row["output"]),
                None if row.get("cached_input") is None else float(row["cached_input"]),
            )
        return cls(out)

    def get(self, model: str) -> Price | None:
        return self.models.get(model)

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float | None:
        price = self.get(model)
        if price is None:
            return None
        cached = max(min(cached_tokens, prompt_tokens), 0)
        fresh = max(prompt_tokens - cached, 0)
        cached_rate = price.input if price.cached_input is None else price.cached_input
        return (fresh * price.input + cached * cached_rate + max(completion_tokens, 0) * price.output) / 1_000_000

    def summarize(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """rows: dicts with model, prompt_tokens, completion_tokens, cached_tokens. Unpriced usage is reported separately."""
        total = 0.0
        unpriced = 0
        by_model: dict[str, dict[str, Any]] = {}
        for r in rows:
            m = str(r.get("model", "?"))
            pt, ct, cc = (
                int(r.get("prompt_tokens", 0)),
                int(r.get("completion_tokens", 0)),
                int(r.get("cached_tokens", 0)),
            )
            c = self.cost(m, pt, ct, cc)
            e = by_model.setdefault(
                m,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "cost_usd": 0.0,
                    "priced": c is not None,
                },
            )
            e["calls"] += 1
            e["prompt_tokens"] += pt
            e["completion_tokens"] += ct
            e["cached_tokens"] += cc
            if c is None:
                unpriced += 1
            else:
                e["cost_usd"] += c
                total += c
        return {"total_usd": round(total, 8), "unpriced_calls": unpriced, "by_model": by_model}
