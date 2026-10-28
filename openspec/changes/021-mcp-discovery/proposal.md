# 021 - MCP discovery and resource lifecycle (Stage 14)

## Why
Roadmap Stage 14: agents discover which tools exist, tool access is negotiated, and connections and resources are managed explicitly (open/close, change subscriptions, stale cleanup). Stage 13 routed tools through a hard-coded map and held connections implicitly.

## What
- `discovery.py` (snapshots, pure `negotiate`), `hub.py` (`McpHub`), `resources.py` (`ResourceCache`), `live.py` and `demo.py`.
- Read-only resources mirroring the tool outputs on the four servers.

## Design decisions
- Negotiation is a pure function over snapshots, so it is testable without a server or a model. Server order is priority order; a tool budget models the context window; required tools beyond the budget are an error, never silently dropped.
- Each connection runs in its own task so it can be closed or reconnected independently; `close` is idempotent and the hub closes everything on exit.
- Live updates use `Client.listen()` (the only mechanism in MCP 2026-07-28). There is no replay: `open()` waits for the stream acknowledgment, a lost stream drops that server's cache entries, retries are bounded (growing delay) and reset only by a stream that delivered an event, then the hub falls back to TTL-only caching.
- Resources mirror tools (same text), so the resource and tool views cannot drift.
- The agent-facing path (`McpToolbox`, prompts, cache keys) is unchanged.

## Out of scope
Using the hub inside agents, HTTP transports and authentication, prompts, resource templates, sampling.

## Acceptance
Discovery equals the Stage 13 map; resource/tool equality for 28 incidents; event handling, stale cleanup, failure injection and lifecycle tests; mutation check; demo output.
