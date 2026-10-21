"""Shared text normalisation for the knowledge layer (BM25, hashing embedder, query building).

One tokenizer for every retriever, so a term means the same thing to BM25 and to the hashing embedder.
Deterministic and dependency-free. Identifiers are kept whole *and* split (``cpu_percent`` ->
``cpu``, ``percent``, ``cpu_percent``; ``OOMKilled`` -> ``oom``, ``killed``) because logs and metric names are
full of them.
"""

from __future__ import annotations

import re

_IDENT = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have if in into is it its may might "
    "more most no not of on or our so such than that the their then there these they this those to was were what "
    "when where which while who will with would you your we us i also any all each per via over under after before "
    "up down out off than too very just only both either neither yet".split()
)

_DOUBLE_OK = ("ll", "ss", "zz")


def stem(token: str) -> str:
    """Conservative suffix stripping: only has to be *consistent* (restart/restarts/restarted/restarting)."""
    t = token
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("sses"):
        return t[:-2]
    for suffix in ("ing", "ed"):
        if len(t) > len(suffix) + 2 and t.endswith(suffix):
            t = t[: -len(suffix)]
            if len(t) > 2 and t[-1] == t[-2] and t[-1] not in "aeiou" and t[-2:] not in _DOUBLE_OK:
                t = t[:-1]
            return t
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def _keep_number(token: str) -> bool:
    """Drop timestamps, counters and ids; keep HTTP status codes, which are real signal (502, 503...)."""
    return len(token) == 3 and token[0] in "45"


def tokenize(text: str) -> list[str]:
    """Lowercased, split, stop-worded and stemmed tokens; order preserved, duplicates kept (term frequency)."""
    out: list[str] = []
    for ident in _IDENT.findall(text):
        if ident.isdigit():
            if _keep_number(ident):
                out.append(ident)
            continue
        parts: list[str] = []
        for chunk in ident.split("_"):
            parts.extend(p for p in _CAMEL.split(chunk) if p)
        words = [stem(p.lower()) for p in parts if not p.isdigit() or _keep_number(p)]
        words = [w for w in words if w and w not in STOPWORDS and len(w) > 1]
        out.extend(words)
        if len(words) > 1:
            out.append(ident.lower())  # the identifier itself: cpu_percent, OOMKilled, p99
    return out


def normalise_message(message: str) -> str:
    """Collapse numbers so log lines that differ only in counters become one template."""
    return re.sub(r"\d+(?:\.\d+)?", "#", message).strip()
