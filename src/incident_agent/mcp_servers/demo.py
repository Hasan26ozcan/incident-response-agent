"""Demo of Stage 14: discovery, context negotiation, live updates, stale cleanup and an explicit close.

    python -m incident_agent.mcp_servers.demo [INC-001]

No API key and no network: every server runs in this process. The output contains no times or ids, so the
transcript in ``docs/EVAL.md`` can be compared with a real run.
"""

from __future__ import annotations

import sys
from typing import Any

import anyio

from incident_agent.mcp_servers.discovery import Needs
from incident_agent.mcp_servers.hub import McpHub
from incident_agent.mcp_servers.live import build_live_server
from incident_agent.mcp_servers.servers import SERVER_KINDS, build_server, resource_uri


async def demo(incident_id: str, out: Any = None) -> McpHub:
    out = out or sys.stdout

    def say(text: str = "") -> None:
        print(text, file=out)

    targets: dict[str, Any] = {k: build_server(k, incident_id) for k in SERVER_KINDS}
    targets["live"] = build_live_server(incident_id)
    feed = resource_uri(incident_id, "feed")
    async with McpHub(targets) as hub:
        say(f"== 1. Discovery ({incident_id}): nothing below was hard-coded in the client")
        for snap in hub.snapshots:
            live = "live updates" if hub.is_live(snap.key) else "TTL only"
            say(f"{snap.key:<8} {snap.server_name}  protocol {snap.protocol_version}  [{live}]")
            say(f"         tools: {', '.join(snap.tool_names)}")
            say(f"         resources: {', '.join(r.uri.split('/', 3)[3] for r in snap.resources)}")

        say("\n== 2. Context negotiation: the agent wants 5 tools but can afford 3 specs")
        needs = Needs(
            required=("search_logs", "get_metric_summary"),
            optional=("run_diagnostic", "annotate", "get_recent_deploys"),
            max_tools=3,
        )
        neg = hub.negotiate(needs)
        say(f"granted: {', '.join(f'{t} <- {neg.routes[t]}' for t in neg.granted)}")
        say(f"dropped for budget: {', '.join(neg.dropped_for_budget) or '-'}")
        say(f"not offered (yet): {', '.join(neg.missing_optional) or '-'}")

        say("\n== 3. A live source changes while the agent works")
        say(f"read feed      -> {await hub.read_resource('live', feed)!r}   (cached: {len(hub.cache)} entry)")
        await hub.call_tool("post_update", {"text": "rollback of deploy started"})
        await hub.wait_for(lambda: ("live", feed) not in hub.cache.keys())
        say("server pushed 'resource updated' -> the cache entry was dropped, not served stale")
        say(f"read feed      -> {await hub.read_resource('live', feed)!r}")

        say("\n== 4. The source gains a tool; the catalog follows")
        await hub.call_tool("enable_annotate")
        await hub.wait_for(lambda: "annotate" in hub.negotiate().routes)
        say(f"annotate is now routed to: {hub.negotiate().routes['annotate']}")

        say("\n== 5. Stale cleanup: the source is decommissioned")
        await hub.read_resource("live", feed)
        await hub.call_tool("retire_feed")
        await hub.wait_for(lambda: len(hub.cache) == 0)
        listed = [r.uri for s in hub.snapshots for r in s.resources]
        say(f"feed still listed: {feed in listed}; cache entries left: {len(hub.cache)}")

        say("\n== 6. Explicit lifecycle")
        await hub.close("live")
        say(f"closed 'live'; second close returns {await hub.close('live')}; open now: {', '.join(hub.open_servers)}")
    say(f"left the context manager: opened {hub.opened}, closed {hub.closed}, still open {len(hub.open_servers)}")
    say("\nevent trail:")
    for line in hub.events:
        say(f"  {line}")
    return hub


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    incident = args[0] if args else "INC-001"
    try:
        anyio.run(demo, incident)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
