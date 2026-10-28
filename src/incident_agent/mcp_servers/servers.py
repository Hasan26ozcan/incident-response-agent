"""The four MCP servers (one per data source), each bound to a single incident.

* ``logs``     get_incident_meta, log_overview, search_logs, log_window
* ``metrics``  get_metric_summary, list_metrics, query_metric
* ``deploys``  get_recent_deploys
* ``sandbox``  list_diagnostics, run_diagnostic (allow-listed read-only scripts in a separate process)

The incident id is fixed when a server is built and is not a tool argument, so a client cannot reach other incidents
or the gold answers. Failures are MCP tool errors (``isError``) whose text starts with ``[bad_args]`` (the caller
should fix the call) or ``[unavailable]`` (the source is down), which ``client.McpToolbox`` maps back to ``ToolError``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError

from incident_agent.mcp_servers.backend import IncidentBackend
from incident_agent.mcp_servers.diagnostics import DEFAULT_TIMEOUT_S, describe_scripts, run_diagnostic
from incident_agent.tools.toolbox import TOOL_SPECS, ToolError

SERVER_KINDS = ("logs", "metrics", "deploys", "sandbox")
BAD_ARGS = "[bad_args] "
UNAVAILABLE = "[unavailable] "
_DESCRIPTIONS = {t["function"]["name"]: t["function"]["description"] for t in TOOL_SPECS}


def resource_uri(incident_id: str, path: str) -> str:
    return f"incident://{incident_id}/{path}"


# Read-only resources per server: (path under incident://<id>/, backend tool whose output the resource mirrors).
SERVER_RESOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "logs": (("meta", "get_incident_meta"), ("logs/overview", "log_overview")),
    "metrics": (("metrics/summary", "get_metric_summary"),),
    "deploys": (("deploys/recent", "get_recent_deploys"),),
    "sandbox": (("diagnostics", "list_diagnostics"),),
}

SERVER_TOOLS: dict[str, tuple[str, ...]] = {
    "logs": ("get_incident_meta", "log_overview", "search_logs", "log_window"),
    "metrics": ("get_metric_summary", "list_metrics", "query_metric"),
    "deploys": ("get_recent_deploys",),
    "sandbox": ("list_diagnostics", "run_diagnostic"),
}


def _guard(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except ToolError as exc:
        raise McpToolError((BAD_ARGS if exc.bad_args else UNAVAILABLE) + exc.reason) from exc


def build_server(kind: str, incident_id: str, *, diagnostic_timeout: float = DEFAULT_TIMEOUT_S) -> MCPServer:
    if kind not in SERVER_KINDS:
        raise ValueError(f"unknown server {kind!r}; choose one of {', '.join(SERVER_KINDS)}")
    backend = IncidentBackend(incident_id)  # raises ValueError for a bad or unknown incident id
    server = MCPServer(
        f"incident-{kind}",
        instructions=f"Read-only {kind} data source for incident {incident_id}. Tools never modify anything.",
    )

    def call(name: str, **args: Any) -> str:
        return _guard(lambda: backend.call(name, args))

    def mirror(path: str, tool: str) -> None:
        """Expose a tool's no-argument output as a read-only resource (same text, so tool and resource cannot drift)."""

        def read() -> str:
            return describe_scripts() if tool == "list_diagnostics" else call(tool)

        server.resource(
            resource_uri(incident_id, path),
            name=path,
            description=f"Same text as the {tool} tool.",
            mime_type="text/plain",
        )(read)

    for path, tool in SERVER_RESOURCES[kind]:
        mirror(path, tool)

    if kind == "logs":

        @server.tool(name="get_incident_meta", description=_DESCRIPTIONS["get_incident_meta"])
        def get_incident_meta() -> str:
            return call("get_incident_meta")

        @server.tool(name="log_overview", description=_DESCRIPTIONS["log_overview"])
        def log_overview() -> str:
            return call("log_overview")

        @server.tool(name="search_logs", description=_DESCRIPTIONS["search_logs"])
        def search_logs(query: str, level: str | None = None, limit: int | None = None) -> str:
            return call("search_logs", query=query, level=level, limit=limit)

        @server.tool(
            name="log_window",
            description="Log lines inside a time window (ISO-8601 start/end, both optional) at an optional level.",
        )
        def log_window(start: str | None = None, end: str | None = None, level: str = "ANY", limit: int = 20) -> str:
            return call("log_window", start=start, end=end, level=level, limit=limit)

    elif kind == "metrics":

        @server.tool(name="get_metric_summary", description=_DESCRIPTIONS["get_metric_summary"])
        def get_metric_summary() -> str:
            return call("get_metric_summary")

        @server.tool(name="list_metrics", description="Metric names with point counts and time span.")
        def list_metrics() -> str:
            return call("list_metrics")

        @server.tool(
            name="query_metric",
            description="One metric's points (downsampled to max_points, the peak always kept) in an optional window.",
        )
        def query_metric(metric: str, start: str | None = None, end: str | None = None, max_points: int = 30) -> str:
            return call("query_metric", metric=metric, start=start, end=end, max_points=max_points)

    elif kind == "deploys":

        @server.tool(name="get_recent_deploys", description=_DESCRIPTIONS["get_recent_deploys"])
        def get_recent_deploys() -> str:
            return call("get_recent_deploys")

    else:

        @server.tool(name="list_diagnostics", description="The allow-listed diagnostic scripts and their parameters.")
        def list_diagnostics() -> str:
            return describe_scripts()

        @server.tool(
            name="run_diagnostic",
            description="Run one allow-listed read-only diagnostic script in an isolated process (no shell, no code).",
        )
        def run_diagnostic_tool(script: str, arguments: dict[str, Any] | None = None) -> str:
            return _guard(lambda: run_diagnostic(incident_id, script, arguments, timeout=diagnostic_timeout))

    return server
