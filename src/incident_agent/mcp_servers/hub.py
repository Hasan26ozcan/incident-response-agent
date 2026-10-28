"""``McpHub``: explicit lifecycle for several MCP connections, with discovery, live updates and stale cleanup.

* **Open/close**: every connection runs in its own task, so one can be closed or reconnected without disturbing the
  others. ``close`` is idempotent; leaving the ``async with`` closes everything, also on errors and cancellation.
  ``opened``/``closed`` count real transitions so tests can prove nothing leaks.
* **Discovery**: each open connection has a ``ServerSnapshot`` (tools, resources, instructions, protocol version);
  ``negotiate`` grants the agent a tool set from them and ``call_tool`` routes by that catalog, not by a hard-coded map.
* **Live updates** (MCP 2026-07-28 ``subscriptions/listen``; the older ``resources/subscribe`` is gone from the SDK):
  a resource-updated event invalidates that cache entry, a tools-changed event refreshes the catalog, a
  resources-changed event refreshes the catalog and deletes cache entries for resources the server stopped listing.
  A dropped stream is re-opened (with a growing delay) up to ``relisten_attempts`` times in a row; there is no
  replay, so the server's cache entries are dropped first. After that, or when the server cannot push events, the
  connection falls back to TTL-only caching.
* **Stale cleanup**: the cache never serves an entry older than its TTL, closing a server drops its entries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from enum import StrEnum
from typing import Any

import anyio
from mcp.client import Client
from mcp.client.subscriptions import SubscriptionLost
from mcp.shared.subscriptions import ResourcesListChanged, ResourceUpdated, ToolsListChanged

from incident_agent.mcp_servers.discovery import Needs, Negotiation, ServerSnapshot, negotiate, take_snapshot
from incident_agent.mcp_servers.resources import ResourceCache


class HubError(RuntimeError):
    pass


class ServerUnavailable(HubError):
    """The server is not connected (never opened, closed, or its connection failed)."""


class ConnState(StrEnum):
    NEW = "new"
    OPEN = "open"
    CLOSED = "closed"
    FAILED = "failed"


class _Conn:
    def __init__(self, key: str, target: Any) -> None:
        self.key = key
        self.target = target
        self.state = ConnState.NEW
        self.client: Any = None
        self.snapshot: ServerSnapshot | None = None
        self.scope: anyio.CancelScope | None = None
        self.done: anyio.Event | None = None
        self.error: BaseException | None = None
        self.live = False  # True while a listen stream is acknowledged
        self.watch_settled: anyio.Event | None = None  # set once the first listen ack arrived or watching was given up


class McpHub:
    def __init__(
        self,
        targets: Mapping[str, Any],
        *,
        cache: ResourceCache | None = None,
        client_factory: Callable[[Any], Any] = Client,
        watch: bool = True,
        relisten_attempts: int = 3,
        relisten_delay: float = 0.1,
    ) -> None:
        if not targets:
            raise ValueError("at least one server is required")
        if relisten_attempts < 0:
            raise ValueError("relisten_attempts cannot be negative")
        self._conns = {k: _Conn(k, t) for k, t in targets.items()}  # dict order = priority order
        self.cache = cache if cache is not None else ResourceCache()
        self._factory = client_factory
        self._watch = watch
        self._relisten_attempts = relisten_attempts
        self._relisten_delay = relisten_delay
        self._tg: Any = None
        self._stack: AsyncExitStack | None = None
        self.opened = 0
        self.closed = 0
        self.relistens = 0
        self.events: list[str] = []  # human-readable trail (used by the demo and the tests)

    # --- lifecycle ----------------------------------------------------------------------------------------------

    async def __aenter__(self) -> McpHub:
        stack = AsyncExitStack()
        try:
            self._tg = await stack.enter_async_context(anyio.create_task_group())
            self._stack = stack
            for key in self._conns:
                await self.open(key)
        except BaseException:
            await self._shutdown(stack)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._shutdown(self._stack)

    async def _shutdown(self, stack: AsyncExitStack) -> None:
        with anyio.CancelScope(shield=True):
            for key in self._conns:
                await self.close(key)
        self._stack = None
        await stack.aclose()

    async def open(self, key: str) -> ServerSnapshot:
        conn = self._conn(key)
        if conn.state is ConnState.OPEN:
            assert conn.snapshot is not None
            return conn.snapshot
        if self._tg is None:
            raise HubError("the hub is not running; use 'async with McpHub(...)'")
        conn.error, conn.client, conn.snapshot, conn.live = None, None, None, False
        conn.done = anyio.Event()
        conn.watch_settled = anyio.Event()
        await self._tg.start(self._run, conn)
        await conn.watch_settled.wait()  # events published before the stream is acknowledged would be lost
        if conn.state is not ConnState.OPEN:
            raise ServerUnavailable(f"{key}: could not connect: {conn.error!r}") from conn.error
        self.opened += 1
        assert conn.snapshot is not None
        self.events.append(f"open {key}: {len(conn.snapshot.tools)} tools, {len(conn.snapshot.resources)} resources")
        return conn.snapshot

    async def close(self, key: str) -> bool:
        """Close one connection. Returns False when it was not open (idempotent)."""
        conn = self._conn(key)
        was_open = conn.state is ConnState.OPEN
        if conn.scope is not None:
            conn.scope.cancel()
        if conn.done is not None:
            await conn.done.wait()
        conn.scope = conn.done = None
        conn.client, conn.live = None, False
        if was_open:
            conn.state = ConnState.CLOSED
            conn.snapshot = None
            self.closed += 1
            self.cache.drop_server(key)
            self.events.append(f"close {key}")
        return was_open

    async def reconnect(self, key: str) -> ServerSnapshot:
        await self.close(key)
        return await self.open(key)

    async def _run(self, conn: _Conn, *, task_status: Any) -> None:
        started = False
        done = conn.done
        assert done is not None
        try:
            with anyio.CancelScope() as scope:
                conn.scope = scope
                async with self._factory(conn.target) as client:
                    conn.client = client
                    conn.snapshot = await take_snapshot(conn.key, client)
                    conn.state = ConnState.OPEN
                    started = True
                    task_status.started()
                    if self._watch and conn.snapshot.supports_listen:
                        await self._watch_loop(conn)
                    assert conn.watch_settled is not None
                    conn.watch_settled.set()
                    await anyio.sleep_forever()
        except Exception as exc:  # a failed connect or a crashed connection must not take the hub down
            conn.error = exc
            if conn.state is ConnState.OPEN:
                self.events.append(f"lost {conn.key}: {type(exc).__name__}")
            conn.state = ConnState.FAILED
            self.cache.drop_server(conn.key)
        finally:
            if not started:
                task_status.started()
            if conn.watch_settled is not None:
                conn.watch_settled.set()
            done.set()

    # --- catalog ------------------------------------------------------------------------------------------------

    def _conn(self, key: str) -> _Conn:
        try:
            return self._conns[key]
        except KeyError:
            raise HubError(f"unknown server {key!r}; known: {', '.join(self._conns)}") from None

    def state(self, key: str) -> ConnState:
        return self._conn(key).state

    def is_live(self, key: str) -> bool:
        return self._conn(key).live

    @property
    def snapshots(self) -> list[ServerSnapshot]:
        return [c.snapshot for c in self._conns.values() if c.state is ConnState.OPEN and c.snapshot is not None]

    @property
    def open_servers(self) -> list[str]:
        return [c.key for c in self._conns.values() if c.state is ConnState.OPEN]

    def negotiate(self, needs: Needs | None = None) -> Negotiation:
        return negotiate(self.snapshots, needs)

    # --- use ----------------------------------------------------------------------------------------------------

    def _open_client(self, key: str) -> Any:
        conn = self._conn(key)
        if conn.state is not ConnState.OPEN:
            raise ServerUnavailable(f"{key} is {conn.state.value}")
        return conn.client

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[str, bool]:
        """Route by the discovered catalog. Returns (text, is_error)."""
        route = negotiate(self.snapshots).routes.get(name)
        if route is None:
            raise HubError(f"no connected server offers tool {name!r}")
        try:
            result = await self._open_client(route).call_tool(name, arguments or {})
        except ServerUnavailable:
            raise
        except Exception as exc:
            raise ServerUnavailable(f"{route}: {type(exc).__name__}: {exc}") from exc
        return "".join(getattr(c, "text", "") for c in result.content), bool(result.is_error)

    async def read_resource(self, key: str, uri: str) -> str:
        client = self._open_client(key)
        snap = self._conn(key).snapshot
        assert snap is not None
        if uri not in {r.uri for r in snap.resources}:
            raise HubError(f"{key} does not list resource {uri!r}")
        cached = self.cache.get((key, uri))
        if cached is not None:
            return cached
        try:
            result = await client.read_resource(uri)
        except Exception as exc:
            raise ServerUnavailable(f"{key}: {type(exc).__name__}: {exc}") from exc
        text = "".join(getattr(c, "text", "") for c in result.contents)
        self.cache.put((key, uri), text)
        return text

    # --- live updates -------------------------------------------------------------------------------------------

    async def _refresh(self, conn: _Conn) -> None:
        conn.snapshot = await take_snapshot(conn.key, conn.client)
        removed = self.cache.keep_only(conn.key, (r.uri for r in conn.snapshot.resources))
        if removed:
            self.events.append(f"stale {conn.key}: dropped {removed} cached resource(s) no longer listed")

    async def _watch_loop(self, conn: _Conn) -> None:
        attempts = 0
        while True:
            assert conn.snapshot is not None
            uris = [r.uri for r in conn.snapshot.resources]
            resubscribe = False
            try:
                async with conn.client.listen(
                    tools_list_changed=True, resources_list_changed=True, resource_subscriptions=uris
                ) as sub:
                    conn.live = True
                    if conn.watch_settled is not None:
                        conn.watch_settled.set()
                    async for event in sub:
                        attempts = 0  # only a stream that delivers something counts as healthy
                        if isinstance(event, ResourceUpdated):
                            if self.cache.invalidate((conn.key, event.uri)):
                                self.events.append(f"updated {conn.key}: {event.uri} (cache entry dropped)")
                            else:
                                self.events.append(f"updated {conn.key}: {event.uri}")
                        elif isinstance(event, ToolsListChanged):
                            await self._refresh(conn)
                            self.events.append(f"tools changed {conn.key}: now {', '.join(conn.snapshot.tool_names)}")
                        elif isinstance(event, ResourcesListChanged):
                            await self._refresh(conn)
                            self.events.append(f"resources changed {conn.key}: {len(conn.snapshot.resources)} listed")
                            resubscribe = True  # the subscription list is fixed per stream: open a new one
                            break
            except SubscriptionLost:
                conn.live = False
                self.cache.drop_server(conn.key)  # no replay: anything may have changed while the stream was down
                attempts += 1
                self.relistens += 1
                self.events.append(f"stream lost {conn.key} (attempt {attempts})")
                if attempts > self._relisten_attempts:
                    self.events.append(f"fallback {conn.key}: TTL-only caching")
                    return
                await anyio.sleep(self._relisten_delay * attempts)  # a stream that dies at once must not spin
                await self._refresh(conn)
                continue
            conn.live = False
            if not resubscribe:
                self.events.append(f"stream closed by {conn.key}: TTL-only caching")
                return

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> None:
        """Wait until ``predicate()`` holds (events arrive asynchronously); raises TimeoutError otherwise."""
        with anyio.fail_after(timeout):
            while not predicate():
                await anyio.sleep(0.005)
