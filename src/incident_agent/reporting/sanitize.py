"""Untrusted-text handling for anything that leaves the system (Stage 15).

Evidence, root causes and recommendations are derived from log lines, which an attacker or a bug can fill with
anything: secrets, Slack mentions, Markdown links, control characters. Everything that is published passes through
these functions first. They are deliberately conservative (over-redacting is safe, leaking is not).
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")
_WS = re.compile(r"\s+")
_SECRETS = (
    re.compile(r"https://hooks\.slack\.com/\S+"),
    re.compile(r"\b(?:sk|gsk|pk|rk)[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
)
_KEYED = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)\S+")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|#!])")


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(REDACTED, text)
    return _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def one_line(text: str, max_len: int = 300) -> str:
    """Redacted, control-free, single-line text cut to ``max_len`` characters (ellipsis counts)."""
    cleaned = _WS.sub(" ", _CTRL.sub(" ", redact(str(text)))).strip()
    if len(cleaned) > max_len:
        return cleaned[: max(0, max_len - 1)].rstrip() + "…"
    return cleaned


def md_text(text: str, max_len: int = 300) -> str:
    """One line of prose with Markdown structure neutralised (no links, headings, tables, emphasis)."""
    return _MD_SPECIAL.sub(r"\\\1", one_line(text, max_len))


def md_code(text: str, max_len: int = 300) -> str:
    """Inline code span; backticks inside are replaced so the span cannot be closed early."""
    return "`" + one_line(text, max_len).replace("`", "'") + "`"


def slack_escape(text: str) -> str:
    """Slack control characters; stops ``<!channel>``, ``<@U123>`` and ``<url|label>`` injection."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_text(text: str, max_len: int = 300) -> str:
    return slack_escape(one_line(text, max_len))
