"""Tool logic behind the MCP servers: incident-scoped, read-only, SDK-independent.

The five original tools are delegated to ``IncidentToolbox`` so that going through MCP returns byte-identical text
(the agents' prompts and the LLM cache stay valid). ``log_window``, ``list_metrics`` and ``query_metric`` are new
and only reachable through the MCP servers; they are not part of the default agent tool list.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from incident_agent.tools import read_logs, read_metrics
from incident_agent.tools.toolbox import MAX_OUTPUT_CHARS, IncidentToolbox, ToolError, _clip, _describe

LEGACY_TOOLS = ("get_incident_meta", "log_overview", "search_logs", "get_metric_summary", "get_recent_deploys")
EXTRA_TOOLS = ("log_window", "list_metrics", "query_metric")
BACKEND_TOOLS = (*LEGACY_TOOLS, *EXTRA_TOOLS)


def _timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be an ISO-8601 timestamp such as 2026-03-01T00:30:00+00:00") from exc
    return value


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _LogWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str | None = None
    end: str | None = None
    level: Literal["ERROR", "WARN", "INFO", "DEBUG", "ANY"] = "ANY"
    limit: int = Field(default=20, ge=1, le=50)

    _ts = field_validator("start", "end")(lambda v: _timestamp(v))

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


class _QueryMetricArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=80)
    start: str | None = None
    end: str | None = None
    max_points: int = Field(default=30, ge=3, le=60)

    _ts = field_validator("start", "end")(lambda v: _timestamp(v))


_EXTRA_MODELS: dict[str, type[BaseModel]] = {
    "log_window": _LogWindowArgs,
    "list_metrics": _NoArgs,
    "query_metric": _QueryMetricArgs,
}


def _parse(ts: str) -> datetime:
    """ISO-8601 to an aware datetime; a timestamp without an offset is read as UTC (the data is all UTC)."""
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _in_range(ts: str, start: str | None, end: str | None) -> bool:
    t = _parse(ts)
    return (start is None or t >= _parse(start)) and (end is None or t <= _parse(end))


class IncidentBackend:
    """All data-source tools for one incident. ``call`` raises ``ToolError`` exactly like ``IncidentToolbox``."""

    def __init__(self, incident_id: str) -> None:
        self._legacy = IncidentToolbox(incident_id)  # validates the id and that the incident exists
        self.incident_id = incident_id

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name in LEGACY_TOOLS:
            return self._legacy.call(name, arguments)
        if name not in _EXTRA_MODELS:
            raise ToolError(name, f"unknown tool; available: {', '.join(BACKEND_TOOLS)}")
        if not isinstance(arguments, dict):
            raise ToolError(name, "arguments were not a valid JSON object; send a JSON object", bad_args=True)
        try:
            args = _EXTRA_MODELS[name].model_validate(arguments).model_dump()
        except ValidationError as exc:
            raise ToolError(name, f"bad arguments ({_describe(exc)})", bad_args=True) from exc
        try:
            return _clip(getattr(self, f"_{name}")(**args))
        except ToolError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(name, f"data source unavailable: {exc}") from exc

    def _log_window(self, start: str | None, end: str | None, level: str, limit: int) -> str:
        hits = [
            e
            for e in read_logs(self.incident_id)
            if _in_range(e.timestamp, start, end) and (level == "ANY" or e.level == level)
        ]
        if not hits:
            return f"no log lines between {start or 'start'} and {end or 'end'} at level {level}"
        lines = [f"{e.timestamp} [{e.service}] {e.level} {e.message}" for e in hits[:limit]]
        return f"{len(hits)} lines (showing {len(lines)}):\n" + "\n".join(lines)

    def _list_metrics(self) -> str:
        metrics = read_metrics(self.incident_id)
        if not metrics:
            raise ToolError("list_metrics", "metrics store is empty")
        return "\n".join(
            f"{name}: {len(series)} points {series[0]['ts']} .. {series[-1]['ts']}"
            for name, series in sorted(metrics.items())
            if series
        )

    def _query_metric(self, metric: str, start: str | None, end: str | None, max_points: int) -> str:
        metrics = read_metrics(self.incident_id)
        if metric not in metrics:
            raise ToolError(
                "query_metric", f"unknown metric {metric!r}; available: {', '.join(sorted(metrics))}", bad_args=True
            )
        series = [p for p in metrics[metric] if _in_range(p["ts"], start, end)]
        if not series:
            return f"{metric}: no points between {start or 'start'} and {end or 'end'}"
        keep = set(range(len(series))) if len(series) <= max_points else self._downsample(series, max_points)
        peak = max(range(len(series)), key=lambda i: series[i]["value"])
        keep.add(peak)
        rows = [f"{series[i]['ts']} {series[i]['value']}" for i in sorted(keep)]
        return (
            f"{metric}: {len(series)} points, showing {len(rows)} (peak {series[peak]['value']}@{series[peak]['ts']})\n"
            + "\n".join(rows)
        )

    @staticmethod
    def _downsample(series: list[dict[str, Any]], n: int) -> set[int]:
        step = (len(series) - 1) / (n - 1)
        return {round(i * step) for i in range(n)}


__all__ = ["BACKEND_TOOLS", "EXTRA_TOOLS", "LEGACY_TOOLS", "MAX_OUTPUT_CHARS", "IncidentBackend"]
