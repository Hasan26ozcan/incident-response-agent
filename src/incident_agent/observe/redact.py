"""Secret redaction for telemetry (Stage 21): what leaves the process in a span must never contain a credential."""

from __future__ import annotations

import re
from typing import Any

_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gsk|csk|ls[v2]|lsv2_pt)_[A-Za-z0-9_]{16,}"),  # Groq, Cerebras, LangSmith style keys
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|[^\s,;&\"']+)"
    ),
)
MARK = "[REDACTED]"
MAX_STR = 2000


def redact(text: str) -> str:
    out = str(text)
    for rx in _RULES[:-1]:
        out = rx.sub(MARK, out)
    return _RULES[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}{MARK}", out)


def clean_value(value: Any, depth: int = 0) -> Any:
    """Make an attribute value safe to store/export: JSON scalars only, redacted and bounded."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= MAX_STR else text[:MAX_STR] + "...[truncated]"
    if isinstance(value, (list, tuple)) and depth < 2:
        return [clean_value(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, dict) and depth < 2:
        return {str(k)[:64]: clean_value(v, depth + 1) for k, v in list(value.items())[:50]}
    return clean_value(repr(value), depth + 1)
