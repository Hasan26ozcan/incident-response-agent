"""Turn an agent's free-text diagnosis into what the durable workflow needs: a category, a service and evidence.

The command table in ``durable.workflows.RULES`` is deliberately small (restart on memory, scale on saturation, flush a
cache, roll back a deploy). Classification here is conservative: when the signals are absent or tie, the category is
``unknown`` and the workflow proposes only a read-only look. A dependency failure maps to ``dependency`` which has no
mutating command at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from incident_agent.agents.tool_loop import hardened
from incident_agent.eval import harness
from incident_agent.sandbox.commands import SERVICE_RE

SIGNALS: dict[str, tuple[str, ...]] = {
    "memory": ("out of memory", "outofmemory", "oom", "memory leak", "heap", "retains memory", "memory grows"),
    "saturation": ("saturat", "cpu", "busy loop", "unbounded loop", "thread starv", "connection pool", "throttl"),
    "cache": ("cache",),
    "bad_deploy": ("deploy", "rollout", "release", "rolled out", "missing config", "keyerror"),
    "dependency": (
        "dependency",
        "upstream",
        "timeout",
        "timed out",
        "downstream",
        "third-party",
        "payments-stub is slow",
    ),
}
FALSE_ALARM = ("false alarm", "no incident", "not an incident", "no real incident", "load test")


def classify(text: str) -> str:
    low = text.lower()
    if any(m in low for m in FALSE_ALARM):
        return "false_alarm"
    scores = {cat: sum(low.count(k) for k in keys) for cat, keys in SIGNALS.items()}
    best = max(scores.values())
    winners = [c for c, n in scores.items() if n == best]
    return winners[0] if best > 0 and len(winners) == 1 else "unknown"


def pick_service(text: str, services: list[str], fallback: str) -> str:
    """The service the diagnosis names first; otherwise the triage suspect. Only well-formed names are returned."""
    hits = sorted((m.start(), s) for s in services if SERVICE_RE.fullmatch(s) for m in re.finditer(re.escape(s), text))
    chosen = hits[0][1] if hits else fallback
    return chosen if SERVICE_RE.fullmatch(chosen or "") else ""


def make_diagnoser(
    agent: str, client: Any, *, harden: bool = True, temperature: float = 0.0
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    with harness.use_client(client):  # the adapter factory captures the client once, here
        adapter = harness.make_adapter(agent, temperature)

    def diagnose(incident_id: str, triage: dict[str, Any]) -> dict[str, Any]:
        with hardened(harden):
            res = adapter(incident_id)
        text = " ".join([res.root_cause, *res.evidence])
        failed = bool(res.error or res.degraded)
        category = "unknown" if failed else classify(text)
        return {
            "category": category,
            "service": pick_service(text, list(triage.get("services") or []), str(triage.get("suspect_service") or "")),
            "evidence": (res.evidence[0] if res.evidence else "")[:200],
            "confidence": "none" if failed else f"agent:{agent}",
            "root_cause": res.root_cause[:600],
            "risk_tier": res.risk_tier,
            "degraded": res.degraded or res.error,
            "source": f"agent:{agent}",
        }

    return diagnose
