"""A deliberately live demo server (not part of the four agent-facing servers).

Real systems change while an agent investigates: a feed gets a new entry, a tool appears. This server simulates that
so discovery, change events and stale cleanup can be shown and tested. ``post_update`` and ``enable_annotate`` stand in
for the outside world; ``retire_feed`` removes a resource the way a decommissioned data source disappears.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer

from incident_agent.mcp_servers.servers import resource_uri


def build_live_server(incident_id: str) -> MCPServer:
    server = MCPServer(f"incident-live-{incident_id}", instructions="Simulated live feed for one incident.")
    entries: list[str] = []
    feed_uri = resource_uri(incident_id, "feed")
    state = {"feed": True}

    def read_feed() -> str:
        return "\n".join(entries) or "(no updates yet)"

    server.resource(feed_uri, name="feed", description="Operator updates, newest last.", mime_type="text/plain")(
        read_feed
    )

    @server.tool(name="post_update", description="Simulate an operator posting an update to the feed.")
    async def post_update(text: str, ctx: Context) -> str:
        if not state["feed"]:
            return "feed is retired"
        entries.append(text)
        await ctx.notify_resource_updated(feed_uri)
        return f"{len(entries)} update(s)"

    @server.tool(name="enable_annotate", description="Simulate the data source gaining a new tool.")
    async def enable_annotate(ctx: Context) -> str:
        def annotate(note: str) -> str:
            return f"annotated: {note}"

        server.add_tool(annotate, name="annotate", description="Attach a note (appears after enable_annotate).")
        await ctx.notify_tools_changed()
        return "annotate enabled"

    @server.tool(name="retire_feed", description="Simulate the data source being decommissioned.")
    async def retire_feed(ctx: Context) -> str:
        server._resource_manager._resources.pop(feed_uri, None)  # no public remove_resource in the SDK
        state["feed"] = False
        await ctx.notify_resources_changed()
        return "feed retired"

    return server
