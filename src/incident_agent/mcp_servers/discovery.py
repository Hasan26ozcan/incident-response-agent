"""Tool/resource discovery and context negotiation.

``take_snapshot`` asks a connected server what it is (name, instructions, negotiated protocol version, tools,
resources). ``negotiate`` is a pure function: given the snapshots and what the agent *needs* (required and optional
tools, and a tool budget for its context window) it decides which tools the agent is granted and which server serves
each. Nothing here talks to a model; the agent never sees a tool the negotiation did not grant.

Protocol-level negotiation (protocol version, server capabilities) is done by the MCP SDK in the handshake; what is
added here is the application-level part on top of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

LISTEN_MIN_VERSION = "2026-07-28"  # first protocol version with subscriptions/listen (ISO dates sort as text)


class NegotiationError(ValueError):
    """The needs cannot be expressed (for example more required tools than the budget allows)."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ResourceInfo:
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class ServerSnapshot:
    key: str  # the hub's name for the server
    server_name: str
    instructions: str
    protocol_version: str
    tools: tuple[ToolInfo, ...]
    resources: tuple[ResourceInfo, ...]

    @property
    def supports_listen(self) -> bool:
        return self.protocol_version >= LISTEN_MIN_VERSION

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tools)


async def take_snapshot(key: str, client: Any) -> ServerSnapshot:
    tools = (await client.list_tools()).tools
    caps = client.server_capabilities
    has_resources = caps is not None and getattr(caps, "resources", None) is not None
    resources = (await client.list_resources()).resources if has_resources else []
    info = client.server_info
    return ServerSnapshot(
        key=key,
        server_name=info.name if info is not None else key,
        instructions=client.instructions or "",
        protocol_version=str(client.protocol_version or ""),
        tools=tuple(ToolInfo(t.name, t.description or "", dict(t.input_schema)) for t in tools),
        resources=tuple(ResourceInfo(str(r.uri), r.name, r.description or "", r.mime_type or "") for r in resources),
    )


@dataclass(frozen=True)
class Needs:
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    max_tools: int | None = None  # how many tool specs the agent's context can afford; None = no limit


@dataclass(frozen=True)
class Negotiation:
    routes: dict[str, str]  # granted tool -> server key
    granted: tuple[str, ...]
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]
    dropped_for_budget: tuple[str, ...]
    conflicts: dict[str, tuple[str, ...]]  # tool offered by several servers -> those servers (first one wins)
    polling_only: tuple[str, ...]  # servers that cannot push change events: their resources rely on the TTL

    @property
    def ok(self) -> bool:
        return not self.missing_required


def negotiate(snapshots: Sequence[ServerSnapshot], needs: Needs | None = None) -> Negotiation:
    """Decide the granted tool set. Server order is priority order when two servers offer the same tool name."""
    offered: dict[str, list[str]] = {}
    for snap in snapshots:
        for name in snap.tool_names:
            offered.setdefault(name, []).append(snap.key)
    conflicts = {n: tuple(s) for n, s in offered.items() if len(s) > 1}
    polling = tuple(s.key for s in snapshots if not s.supports_listen)

    if needs is None:
        wanted_required: list[str] = []
        wanted_optional = list(offered)
        budget = None
    else:
        wanted_required = list(dict.fromkeys(needs.required))
        wanted_optional = [n for n in dict.fromkeys(needs.optional) if n not in wanted_required]
        budget = needs.max_tools
        if budget is not None and budget < 0:
            raise NegotiationError("max_tools cannot be negative")
        if budget is not None and len(wanted_required) > budget:
            raise NegotiationError(f"{len(wanted_required)} required tools do not fit a budget of {budget}")

    missing_required = tuple(n for n in wanted_required if n not in offered)
    granted = [n for n in wanted_required if n in offered]
    missing_optional: list[str] = []
    dropped: list[str] = []
    for name in wanted_optional:
        if name not in offered:
            missing_optional.append(name)
        elif budget is not None and len(granted) >= budget:
            dropped.append(name)
        else:
            granted.append(name)
    return Negotiation(
        routes={n: offered[n][0] for n in granted},
        granted=tuple(granted),
        missing_required=missing_required,
        missing_optional=tuple(missing_optional),
        dropped_for_budget=tuple(dropped),
        conflicts=conflicts,
        polling_only=polling,
    )
