# 020 - MCP servers (Stage 13)

## Why
Roadmap Stage 13: log, metrics, deploy and sandbox-script access, each as its own MCP server, so agents pull data from (simulated) live systems through a standard protocol instead of in-process function calls.

## What
- `mcp_servers/backend.py` (tool logic, SDK-independent), `diagnostics.py` (allow-listed scripts in an isolated process), `servers.py` (four MCP servers), `client.py` (`McpToolbox`), `__main__.py` (stdio entry point).
- `LLMReActAgent(toolbox_factory)` and `--toolbox mcp` in the harness.

## Design decisions
- Incident scope is fixed when a server is built and is never a tool argument; gold answers are unreachable.
- The five original tools delegate to `IncidentToolbox`; model-facing specs stay `TOOL_SPECS`. Result: identical prompts, identical cache keys, identical diagnoses; the MCP layer is provably transparent.
- New tools (`log_window`, `list_metrics`, `query_metric`, `list_diagnostics`, `run_diagnostic`) are MCP-only and opt-in (`extended=True`); the default agent tool list is unchanged.
- The sandbox server runs a fixed registry of read-only scripts, never a shell or code. Arbitrary command execution and escape testing belong to Stage 16.
- Errors are MCP tool errors tagged `[bad_args]` / `[unavailable]`; the client maps them to `ToolError(bad_args=...)`.
- Pinned to `mcp>=2.2,<3`: the 2.x API (`MCPServer`, `Client`) differs from 1.x `FastMCP`.

## Out of scope
Discovery and resource lifecycle (Stage 14), real remote services, authentication, wiring the other agents.

## Acceptance
28-incident parity, identical LLM requests through MCP, stdio round trip, timeout/error mapping, mutation check.
