"""``McpToolbox``: the ``IncidentToolbox`` interface served by the MCP servers.

Same constructor idea (``incident_id``, ``fail_tools``, ``allowed_tools``), same ``call(name, arguments) -> str`` and
the same ``ToolError`` semantics, so an agent can use either. By default only the five original tools are exposed and
their model-facing specs are the unchanged ``TOOL_SPECS`` (prompts and the LLM cache stay valid); ``extended=True``
adds the MCP-only tools. Servers are connected in-process (``transport="memory"``, used by the evaluation) or as real
stdio subprocesses (``transport="stdio"``, what any external MCP client would do).

The toolbox is synchronous: a private event loop runs in a background thread and owns the MCP sessions. Call
``close()`` (or use ``with``) when done; sessions are opened lazily on the first call.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import ExitStack
from typing import Any

import anyio.from_thread
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from incident_agent.mcp_servers.backend import EXTRA_TOOLS, LEGACY_TOOLS, IncidentBackend
from incident_agent.mcp_servers.diagnostics import DEFAULT_TIMEOUT_S, SRC_ROOT
from incident_agent.mcp_servers.servers import BAD_ARGS, SERVER_KINDS, SERVER_TOOLS, UNAVAILABLE, build_server
from incident_agent.tools.toolbox import TOOL_SPECS, ToolError

SANDBOX_TOOLS = ("list_diagnostics", "run_diagnostic")
EXTENDED_TOOLS = (*EXTRA_TOOLS, *SANDBOX_TOOLS)
_TAGGED = re.compile(r"^Error executing tool \S+: \[(bad_args|unavailable)\] (.*)$", re.DOTALL)
_SERVER_OF = {tool: kind for kind, tools in SERVER_TOOLS.items() for tool in tools}


def _stdio_params(kind: str, incident_id: str) -> StdioServerParameters:
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    env["PYTHONPATH"] = str(SRC_ROOT)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "incident_agent.mcp_servers", kind, "--incident", incident_id],
        env=env,
    )


def _spec_from_mcp(tool: Any) -> dict[str, Any]:
    schema = {k: v for k, v in tool.input_schema.items() if k != "title"}
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description or "", "parameters": schema},
    }


class McpToolbox:
    def __init__(
        self,
        incident_id: str,
        fail_tools: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        *,
        extended: bool = False,
        transport: str = "memory",
        diagnostic_timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if transport not in ("memory", "stdio"):
            raise ValueError("transport must be 'memory' or 'stdio'")
        IncidentBackend(incident_id)  # same ValueError as IncidentToolbox for a bad or unknown incident
        self.incident_id = incident_id
        self.extended = extended
        self.transport = transport
        self.diagnostic_timeout = diagnostic_timeout
        self.tool_names = [*LEGACY_TOOLS, *(EXTENDED_TOOLS if extended else ())]
        self._fail = fail_tools or set()
        self.allowed = set(self.tool_names) if allowed_tools is None else set(allowed_tools) & set(self.tool_names)
        self._stack: ExitStack | None = None
        self._portal: Any = None
        self._clients: dict[str, Client] = {}

    # --- lifecycle ---------------------------------------------------------------------------------------------

    def _open(self) -> None:
        if self._stack is not None:
            return
        stack = ExitStack()
        try:
            portal = stack.enter_context(anyio.from_thread.start_blocking_portal())
            kinds = SERVER_KINDS if self.extended else ("logs", "metrics", "deploys")
            for kind in kinds:
                target: Any = (
                    build_server(kind, self.incident_id, diagnostic_timeout=self.diagnostic_timeout)
                    if self.transport == "memory"
                    else _stdio_params(kind, self.incident_id)
                )
                self._clients[kind] = stack.enter_context(portal.wrap_async_context_manager(Client(target)))
        except BaseException:
            stack.close()
            self._clients.clear()
            raise
        self._stack, self._portal = stack, portal

    def close(self) -> None:
        stack, self._stack, self._portal = self._stack, None, None
        self._clients.clear()
        if stack is not None:
            stack.close()

    def __enter__(self) -> McpToolbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- the IncidentToolbox interface -------------------------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        """Model-facing specs of the allowed tools: ``TOOL_SPECS`` for the originals, MCP schemas for the rest."""
        legacy = [t for t in TOOL_SPECS if t["function"]["name"] in self.allowed]
        extra = [n for n in self.tool_names if n in self.allowed and n not in LEGACY_TOOLS]
        if not extra:
            return legacy
        self._open()
        found: dict[str, Any] = {}
        for kind in SERVER_KINDS:
            if kind in self._clients:
                for tool in self._portal.call(self._clients[kind].list_tools).tools:
                    found[tool.name] = _spec_from_mcp(tool)
        return legacy + [found[n] for n in extra if n in found]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self.tool_names:
            raise ToolError(name, f"unknown tool; available: {', '.join(self.tool_names)}")
        if name not in self.allowed:
            raise ToolError(name, f"not available to you; your tools: {', '.join(sorted(self.allowed))}", bad_args=True)
        if name in self._fail:
            raise ToolError(name, "data source unavailable (injected failure)")
        if not isinstance(arguments, dict) or "_raw" in arguments:
            raise ToolError(name, "arguments were not a valid JSON object; send a JSON object", bad_args=True)
        self._open()
        client = self._clients[_SERVER_OF[name]]
        try:
            result = self._portal.call(client.call_tool, name, arguments)
        except Exception as exc:  # transport or protocol failure: the source is unreachable, not the caller's fault
            raise ToolError(name, f"data source unavailable: {type(exc).__name__}: {exc}") from exc
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise self._to_tool_error(name, text)
        return text

    @staticmethod
    def _to_tool_error(name: str, text: str) -> ToolError:
        tagged = _TAGGED.match(text)
        if tagged:
            return ToolError(name, tagged.group(2), bad_args=tagged.group(1) == "bad_args")
        if text.startswith("Unknown tool"):
            return ToolError(name, text)
        if text.startswith("Error executing tool") and ":" in text:
            # argument validation by the MCP layer: the caller's mistake
            return ToolError(name, f"bad arguments ({text.split(': ', 1)[1]})", bad_args=True)
        return ToolError(name, f"internal error: {text}")


__all__ = ["BAD_ARGS", "EXTENDED_TOOLS", "UNAVAILABLE", "McpToolbox"]
