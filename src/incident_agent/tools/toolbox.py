"""LLM-facing, incident-scoped read-only tools (all LOW risk per openspec/risk-classification.md).

The incident id is bound at construction and is NOT a tool argument, so the
model cannot read other incidents or anything under eval/gold/.
"""

from __future__ import annotations

import json
import re
from typing import Any

from incident_agent.tools import PROJECT_ROOT, read_deploys, read_logs, read_meta, read_metrics

MAX_OUTPUT_CHARS = 3500
_ID = re.compile(r"^INC-\d{3}$")


class ToolError(Exception):
    """A tool could not produce a usable result (unknown tool, bad args, source down)."""

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


def _fn(
    name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _fn("get_incident_meta", "Alert metadata: time window, services, severity, alert text."),
    _fn("log_overview", "Log counts per service/level and the most frequent ERROR/WARN message templates."),
    _fn(
        "search_logs",
        "Case-insensitive substring search over log lines.",
        {
            "query": {"type": "string", "description": "substring to look for"},
            "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG", "ANY"]},
            "limit": {"type": "integer", "description": "max lines (<=25)"},
        },
        ["query"],
    ),
    _fn("get_metric_summary", "Per metric: baseline average, peak value and time, last value, anomaly start."),
    _fn("get_recent_deploys", "Deploys and config changes shortly before the incident window."),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SPECS]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"


class IncidentToolbox:
    def __init__(self, incident_id: str, fail_tools: set[str] | None = None) -> None:
        if not _ID.match(incident_id):
            raise ValueError(f"invalid incident id: {incident_id!r}")
        if not (PROJECT_ROOT / "data" / "incidents" / incident_id).is_dir():
            raise ValueError(f"unknown incident: {incident_id}")
        self.incident_id = incident_id
        self._fail = fail_tools or set()  # fault injection for recovery tests

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in TOOL_NAMES:
            raise ToolError(name, f"unknown tool; available: {', '.join(TOOL_NAMES)}")
        if name in self._fail:
            raise ToolError(name, "data source unavailable (injected failure)")
        try:
            return _clip(getattr(self, f"_{name}")(**arguments))
        except TypeError as exc:
            raise ToolError(name, f"bad arguments: {exc}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(name, f"data source unavailable: {exc}") from exc

    def _get_incident_meta(self) -> str:
        return json.dumps(read_meta(self.incident_id), indent=1)

    def _log_overview(self) -> str:
        logs = read_logs(self.incident_id)
        if not logs:
            raise ToolError("log_overview", "log store is empty")
        counts: dict[tuple[str, str], int] = {}
        templates: dict[tuple[str, str], int] = {}
        for e in logs:
            counts[(e.service, e.level)] = counts.get((e.service, e.level), 0) + 1
            if e.level in ("ERROR", "WARN"):
                key = (e.level, re.sub(r"\d+(\.\d+)?", "N", e.message))
                templates[key] = templates.get(key, 0) + 1
        out = [f"{len(logs)} lines, {logs[0].timestamp} .. {logs[-1].timestamp}", "counts (service level n):"]
        out += [f"  {s} {lv} {n}" for (s, lv), n in sorted(counts.items())]
        out.append("top ERROR/WARN templates (count, level, message):")
        for (lv, msg), n in sorted(templates.items(), key=lambda kv: -kv[1])[:12]:
            first = next(e for e in logs if e.level == lv and re.sub(r"\d+(\.\d+)?", "N", e.message) == msg)
            out.append(f"  {n}x {lv} first@{first.timestamp} [{first.service}] {first.message}")
        return "\n".join(out)

    def _search_logs(self, query: str, level: str = "ANY", limit: int = 15) -> str:
        limit = max(1, min(int(limit), 25))
        q = query.lower()
        hits = [
            e for e in read_logs(self.incident_id) if q in e.message.lower() and (level == "ANY" or e.level == level)
        ]
        if not hits:
            return f"no log lines match {query!r} at level {level}"
        lines = [f"{e.timestamp} [{e.service}] {e.level} {e.message}" for e in hits[:limit]]
        return f"{len(hits)} matches (showing {len(lines)}):\n" + "\n".join(lines)

    def _get_metric_summary(self) -> str:
        metrics = read_metrics(self.incident_id)
        if not metrics:
            raise ToolError("get_metric_summary", "metrics store is empty")
        out = []
        for name, series in metrics.items():
            if not series:
                continue
            base = [p["value"] for p in series[: max(1, len(series) // 5)]]
            avg = sum(base) / len(base)
            peak = max(series, key=lambda p: p["value"])
            start = next((p["ts"] for p in series if p["value"] > avg * 2 + 1e-9), None)
            out.append(
                f"{name}: baseline_avg={avg:.2f} peak={peak['value']}@{peak['ts']} "
                f"last={series[-1]['value']} anomaly_start={start}"
            )
        return "\n".join(out)

    def _get_recent_deploys(self) -> str:
        deploys = read_deploys(self.incident_id)
        start = read_meta(self.incident_id)["window_start"]
        recent = [d for d in deploys if d["timestamp"] < start]
        if not recent:
            return "no deploys or config changes before the incident window"
        return json.dumps(recent[-8:], indent=1)
