"""Stage 14: discovery, context negotiation, resource cache, hub lifecycle, live updates, stale cleanup."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
from mcp.client.subscriptions import SubscriptionLost  # noqa: E402
from mcp.shared.subscriptions import ResourcesListChanged, ResourceUpdated, ToolsListChanged  # noqa: E402

from incident_agent.mcp_servers.client import _stdio_params  # noqa: E402
from incident_agent.mcp_servers.discovery import (  # noqa: E402
    LISTEN_MIN_VERSION,
    Needs,
    NegotiationError,
    ServerSnapshot,
    ToolInfo,
    negotiate,
    take_snapshot,
)
from incident_agent.mcp_servers.hub import ConnState, HubError, McpHub, ServerUnavailable  # noqa: E402
from incident_agent.mcp_servers.live import build_live_server  # noqa: E402
from incident_agent.mcp_servers.resources import ResourceCache  # noqa: E402
from incident_agent.mcp_servers.servers import (  # noqa: E402
    SERVER_KINDS,
    SERVER_RESOURCES,
    SERVER_TOOLS,
    build_server,
    resource_uri,
)

ALL_IDS = [f"INC-{i:03d}" for i in range(1, 29)]
INC = "INC-001"


def run(fn: Any) -> Any:
    return anyio.run(fn)


def agent_servers(incident: str = INC) -> dict[str, Any]:
    return {k: build_server(k, incident) for k in SERVER_KINDS}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- ResourceCache ---


class TestResourceCache:
    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            ResourceCache(ttl=0)
        with pytest.raises(ValueError):
            ResourceCache(ttl=-1)
        with pytest.raises(ValueError):
            ResourceCache(max_entries=0)

    def test_hit_and_miss_counters(self) -> None:
        c = ResourceCache()
        assert c.get(("s", "u")) is None
        c.put(("s", "u"), "text")
        assert c.get(("s", "u")) == "text"
        assert (c.hits, c.misses) == (1, 1)
        assert len(c) == 1

    def test_entry_expires_exactly_at_ttl(self) -> None:
        clock = Clock()
        c = ResourceCache(ttl=10, clock=clock)
        c.put(("s", "u"), "x")
        clock.now += 9.999
        assert c.get(("s", "u")) == "x"
        clock.now += 0.001  # age is now exactly ttl
        assert c.get(("s", "u")) is None
        assert c.expired == 1
        assert len(c) == 0

    def test_put_refreshes_the_clock(self) -> None:
        clock = Clock()
        c = ResourceCache(ttl=10, clock=clock)
        c.put(("s", "u"), "old")
        clock.now += 8
        c.put(("s", "u"), "new")
        clock.now += 8
        assert c.get(("s", "u")) == "new"

    def test_lru_eviction_keeps_recently_used(self) -> None:
        c = ResourceCache(max_entries=2)
        c.put(("s", "a"), "1")
        c.put(("s", "b"), "2")
        assert c.get(("s", "a")) == "1"  # a is now the most recent
        c.put(("s", "c"), "3")  # evicts b
        assert c.get(("s", "b")) is None
        assert c.get(("s", "a")) == "1"
        assert c.evicted == 1
        assert len(c) == 2

    def test_invalidate(self) -> None:
        c = ResourceCache()
        c.put(("s", "u"), "x")
        assert c.invalidate(("s", "u")) is True
        assert c.invalidate(("s", "u")) is False
        assert c.invalidated == 1
        assert c.get(("s", "u")) is None

    def test_drop_server_only_touches_that_server(self) -> None:
        c = ResourceCache()
        c.put(("a", "1"), "x")
        c.put(("a", "2"), "x")
        c.put(("b", "1"), "x")
        assert c.drop_server("a") == 2
        assert c.keys() == [("b", "1")]
        assert c.invalidated == 2

    def test_keep_only_drops_unlisted(self) -> None:
        c = ResourceCache()
        c.put(("a", "1"), "x")
        c.put(("a", "2"), "x")
        c.put(("b", "9"), "x")  # another server: untouched even though 9 is not in the list
        assert c.keep_only("a", ["1"]) == 1
        assert sorted(c.keys()) == [("a", "1"), ("b", "9")]
        assert c.invalidated == 1

    def test_sweep_removes_every_expired_entry(self) -> None:
        clock = Clock()
        c = ResourceCache(ttl=10, clock=clock)
        c.put(("s", "old1"), "x")
        c.put(("s", "old2"), "x")
        clock.now += 6
        c.put(("s", "young"), "x")
        clock.now += 5  # old ones are 11 old, young is 5
        assert c.sweep() == 2
        assert c.keys() == [("s", "young")]
        assert c.expired == 2
        assert c.sweep() == 0


# --- negotiate ---


def snap(key: str, tools: list[str], version: str = LISTEN_MIN_VERSION) -> ServerSnapshot:
    return ServerSnapshot(key, f"srv-{key}", "", version, tuple(ToolInfo(t, t) for t in tools), ())


class TestNegotiate:
    def test_no_needs_grants_everything_in_server_order(self) -> None:
        n = negotiate([snap("a", ["x", "y"]), snap("b", ["z"])])
        assert n.granted == ("x", "y", "z")
        assert n.routes == {"x": "a", "y": "a", "z": "b"}
        assert n.ok and not n.missing_required and not n.conflicts

    def test_required_first_then_optional(self) -> None:
        n = negotiate([snap("a", ["x", "y", "z"])], Needs(required=("z",), optional=("x", "y")))
        assert n.granted == ("z", "x", "y")

    def test_missing_required_makes_it_not_ok(self) -> None:
        n = negotiate([snap("a", ["x"])], Needs(required=("x", "nope")))
        assert n.missing_required == ("nope",)
        assert not n.ok
        assert n.granted == ("x",)

    def test_missing_optional_is_reported_but_ok(self) -> None:
        n = negotiate([snap("a", ["x"])], Needs(required=("x",), optional=("nope",)))
        assert n.ok
        assert n.missing_optional == ("nope",)

    def test_budget_drops_optional_in_order(self) -> None:
        n = negotiate(
            [snap("a", ["r", "o1", "o2", "o3"])], Needs(required=("r",), optional=("o1", "o2", "o3"), max_tools=2)
        )
        assert n.granted == ("r", "o1")
        assert n.dropped_for_budget == ("o2", "o3")
        assert len(n.granted) == 2

    def test_budget_exactly_filled_by_required(self) -> None:
        n = negotiate([snap("a", ["r1", "r2", "o"])], Needs(required=("r1", "r2"), optional=("o",), max_tools=2))
        assert n.granted == ("r1", "r2")
        assert n.dropped_for_budget == ("o",)

    def test_required_over_budget_is_an_error(self) -> None:
        with pytest.raises(NegotiationError):
            negotiate([snap("a", ["r1", "r2"])], Needs(required=("r1", "r2"), max_tools=1))

    def test_negative_budget_is_an_error(self) -> None:
        with pytest.raises(NegotiationError):
            negotiate([snap("a", ["x"])], Needs(optional=("x",), max_tools=-1))

    def test_zero_budget_grants_nothing(self) -> None:
        n = negotiate([snap("a", ["x"])], Needs(optional=("x",), max_tools=0))
        assert n.granted == () and n.dropped_for_budget == ("x",)

    def test_duplicate_names_in_needs_are_collapsed(self) -> None:
        n = negotiate([snap("a", ["x"])], Needs(required=("x", "x"), optional=("x",)))
        assert n.granted == ("x",)

    def test_conflict_first_server_wins_and_is_reported(self) -> None:
        n = negotiate([snap("a", ["t"]), snap("b", ["t", "u"])])
        assert n.routes["t"] == "a"
        assert n.routes["u"] == "b"
        assert n.conflicts == {"t": ("a", "b")}

    def test_polling_only_lists_servers_without_listen(self) -> None:
        n = negotiate([snap("new", ["x"]), snap("old", ["y"], version="2025-11-25")])
        assert n.polling_only == ("old",)

    def test_listen_version_boundary(self) -> None:
        assert snap("s", [], LISTEN_MIN_VERSION).supports_listen
        assert not snap("s", [], "2026-07-27").supports_listen
        assert snap("s", [], "2026-07-29").supports_listen

    def test_empty_snapshots(self) -> None:
        n = negotiate([], Needs(required=("x",)))
        assert not n.ok and n.granted == ()


# --- discovery against the real servers ---


class TestSnapshot:
    def test_every_server_reports_its_tools_and_resources(self) -> None:
        async def main() -> None:
            from mcp.client import Client

            for kind in SERVER_KINDS:
                async with Client(build_server(kind, INC)) as client:
                    snap = await take_snapshot(kind, client)
                    assert set(snap.tool_names) == set(SERVER_TOOLS[kind])
                    assert {r.uri for r in snap.resources} == {resource_uri(INC, p) for p, _ in SERVER_RESOURCES[kind]}
                    assert snap.key == kind
                    assert snap.server_name == f"incident-{kind}"
                    assert INC in snap.instructions
                    assert snap.protocol_version >= LISTEN_MIN_VERSION
                    assert all(t.description and t.schema for t in snap.tools)

        run(main)

    def test_resource_text_equals_the_tool_output_for_all_incidents(self) -> None:
        from incident_agent.mcp_servers.backend import IncidentBackend
        from incident_agent.mcp_servers.diagnostics import describe_scripts

        async def main() -> None:
            for inc in ALL_IDS:
                backend = IncidentBackend(inc)
                async with McpHub(agent_servers(inc), watch=False) as hub:
                    for kind, entries in SERVER_RESOURCES.items():
                        for path, tool in entries:
                            text = await hub.read_resource(kind, resource_uri(inc, path))
                            expected = describe_scripts() if tool == "list_diagnostics" else backend.call(tool, {})
                            assert text == expected, (inc, path)

        run(main)

    def test_discovery_routes_match_the_stage13_map(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                routes = hub.negotiate().routes
                expected = {tool: kind for kind, tools in SERVER_TOOLS.items() for tool in tools}
                assert routes == expected

        run(main)


# --- hub lifecycle ---


class TestHubLifecycle:
    def test_requires_servers(self) -> None:
        with pytest.raises(ValueError):
            McpHub({})
        with pytest.raises(ValueError):
            McpHub(agent_servers(), relisten_attempts=-1)

    def test_open_and_close_counts_match_and_everything_is_closed_after_exit(self) -> None:
        hub = McpHub(agent_servers())

        async def main() -> None:
            async with hub:
                assert hub.open_servers == list(SERVER_KINDS)
                assert hub.opened == 4 and hub.closed == 0
                assert all(hub.state(k) is ConnState.OPEN for k in SERVER_KINDS)

        run(main)
        assert hub.opened == hub.closed == 4
        assert all(hub.state(k) is ConnState.CLOSED for k in SERVER_KINDS)
        assert hub.snapshots == []

    def test_close_is_idempotent_and_does_not_disturb_the_others(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                assert await hub.close("metrics") is True
                assert await hub.close("metrics") is False
                assert hub.closed == 1
                assert hub.open_servers == ["logs", "deploys", "sandbox"]
                text, err = await hub.call_tool("get_recent_deploys")
                assert not err and text
                with pytest.raises(HubError, match="no connected server offers"):
                    await hub.call_tool("get_metric_summary")  # its server is closed, so the catalog lost the tool

        run(main)

    def test_reconnect_reopens_a_closed_server(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                await hub.close("deploys")
                snap = await hub.reconnect("deploys")
                assert snap.key == "deploys"
                assert hub.state("deploys") is ConnState.OPEN
                assert hub.opened == 5 and hub.closed == 1
                text, err = await hub.call_tool("get_recent_deploys")
                assert text and not err

        run(main)

    def test_reopen_of_open_server_is_a_no_op(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                before = hub.opened
                await hub.open("logs")
                assert hub.opened == before

        run(main)

    def test_unknown_server_key(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                with pytest.raises(HubError):
                    hub.state("nope")
                with pytest.raises(HubError):
                    await hub.read_resource("nope", "x")

        run(main)

    def test_open_before_start_is_refused(self) -> None:
        async def main() -> None:
            hub = McpHub(agent_servers())
            with pytest.raises(HubError):
                await hub.open("logs")

        run(main)

    def test_everything_is_closed_when_the_body_raises(self) -> None:
        hub = McpHub(agent_servers())

        async def main() -> None:
            async with hub:
                raise KeyError("boom")

        with pytest.raises(BaseException):  # noqa: B017  (exception group or KeyError depending on the runtime)
            run(main)
        assert hub.opened == hub.closed == 4
        assert hub.open_servers == []

    def test_failed_connect_marks_failed_and_leaves_the_others_usable(self) -> None:
        from mcp.client import Client

        class Boom:
            async def __aenter__(self) -> Any:
                raise ConnectionError("refused")

            async def __aexit__(self, *a: object) -> None:
                return None

        flaky = {"down": False}

        def factory(target: Any) -> Any:
            return Boom() if flaky["down"] else Client(target)

        async def main() -> None:
            targets = {"logs": build_server("logs", INC), "deploys": build_server("deploys", INC)}
            async with McpHub(targets, client_factory=factory) as hub:
                await hub.close("logs")
                flaky["down"] = True
                with pytest.raises(ServerUnavailable, match="refused"):
                    await hub.open("logs")
                assert hub.state("logs") is ConnState.FAILED
                assert hub.open_servers == ["deploys"]
                flaky["down"] = False
                text, err = await hub.call_tool("get_recent_deploys")
                assert text and not err
                await hub.open("logs")  # recovers once the source is back
                assert hub.state("logs") is ConnState.OPEN
            assert hub.opened == hub.closed

        run(main)

    def test_failure_while_starting_closes_what_was_opened(self) -> None:
        from mcp.client import Client

        class Boom:
            async def __aenter__(self) -> Any:
                raise ConnectionError("refused")

            async def __aexit__(self, *a: object) -> None:
                return None

        hub = McpHub(
            {"logs": build_server("logs", INC), "bad": "x"},
            client_factory=lambda t: Boom() if t == "x" else Client(t),
        )

        async def main() -> None:
            with pytest.raises(ServerUnavailable):
                async with hub:
                    pass

        run(main)
        assert hub.state("bad") is ConnState.FAILED
        assert hub.state("logs") is ConnState.CLOSED
        assert hub.opened == hub.closed == 1

    def test_calls_to_a_failed_or_closed_server_raise_unavailable(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                await hub.close("logs")
                with pytest.raises(ServerUnavailable):
                    await hub.read_resource("logs", resource_uri(INC, "meta"))

        run(main)


# --- tools and resources through the hub ---


class TestHubUse:
    def test_call_tool_is_routed_by_the_catalog(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                text, err = await hub.call_tool("get_incident_meta")
                assert not err and INC in text
                text, err = await hub.call_tool("search_logs", {"query": "ERROR"})
                assert not err

        run(main)

    def test_tool_error_is_returned_not_raised(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                text, err = await hub.call_tool("search_logs", {"query": ""})
                assert err and "[bad_args]" in text

        run(main)

    def test_unknown_tool(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                with pytest.raises(HubError):
                    await hub.call_tool("no_such_tool")

        run(main)

    def test_read_resource_is_cached_then_expires(self) -> None:
        clock = Clock()
        cache = ResourceCache(ttl=30, clock=clock)

        async def main() -> None:
            async with McpHub(agent_servers(), cache=cache) as hub:
                uri = resource_uri(INC, "meta")
                first = await hub.read_resource("logs", uri)
                assert (cache.hits, cache.misses) == (0, 1)
                assert await hub.read_resource("logs", uri) == first
                assert cache.hits == 1
                clock.now += 31
                assert await hub.read_resource("logs", uri) == first
                assert cache.expired == 1 and cache.misses == 2

        run(main)

    def test_unlisted_resource_is_refused(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                for uri in ("incident://INC-002/meta", resource_uri(INC, "metrics/summary")):
                    # another incident's resource / a resource that lives on another server
                    with pytest.raises(HubError, match="does not list") as exc:
                        await hub.read_resource("logs", uri)
                    assert not isinstance(exc.value, ServerUnavailable)  # refused by the hub, never sent to the server

        run(main)

    def test_closing_a_server_drops_its_cache_entries(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers()) as hub:
                await hub.read_resource("logs", resource_uri(INC, "meta"))
                await hub.read_resource("metrics", resource_uri(INC, "metrics/summary"))
                assert len(hub.cache) == 2
                await hub.close("logs")
                assert hub.cache.keys() == [("metrics", resource_uri(INC, "metrics/summary"))]

        run(main)

    def test_server_is_fixed_to_one_incident(self) -> None:
        async def main() -> None:
            async with McpHub(agent_servers("INC-003")) as hub:
                snap = next(s for s in hub.snapshots if s.key == "logs")
                assert all("INC-003" in r.uri for r in snap.resources)
                assert not any("INC-001" in r.uri for s in hub.snapshots for r in s.resources)

        run(main)


# --- live updates, stale cleanup ---


def live_targets() -> dict[str, Any]:
    return {"logs": build_server("logs", INC), "live": build_live_server(INC)}


FEED = resource_uri(INC, "feed")


class TestLiveUpdates:
    def test_feed_update_invalidates_exactly_that_cache_entry(self) -> None:
        async def main() -> None:
            async with McpHub(live_targets()) as hub:
                assert hub.is_live("live")
                other = resource_uri(INC, "meta")
                assert await hub.read_resource("live", FEED) == "(no updates yet)"
                meta = await hub.read_resource("logs", other)
                _, err = await hub.call_tool("post_update", {"text": "rolled back"})
                assert not err
                await hub.wait_for(lambda: hub.cache.invalidated >= 1)
                assert ("live", FEED) not in hub.cache.keys()
                assert ("logs", other) in hub.cache.keys()  # unrelated entries survive
                assert await hub.read_resource("live", FEED) == "rolled back"
                assert await hub.read_resource("logs", other) == meta

        run(main)

    def test_without_events_the_cache_would_serve_stale_data(self) -> None:
        # the same scenario with watching disabled: documents what the live updates are for
        async def main() -> None:
            async with McpHub(live_targets(), watch=False) as hub:
                assert not hub.is_live("live")
                await hub.read_resource("live", FEED)
                await hub.call_tool("post_update", {"text": "x"})
                await anyio.sleep(0.05)
                assert await hub.read_resource("live", FEED) == "(no updates yet)"  # stale until the TTL

        run(main)

    def test_tools_changed_refreshes_the_catalog(self) -> None:
        async def main() -> None:
            async with McpHub(live_targets()) as hub:
                assert "annotate" not in hub.negotiate().routes
                await hub.call_tool("enable_annotate")
                await hub.wait_for(lambda: "annotate" in hub.negotiate().routes)
                text, err = await hub.call_tool("annotate", {"note": "hi"})
                assert text == "annotated: hi" and not err

        run(main)

    def test_resources_changed_removes_stale_entries_and_listing(self) -> None:
        async def main() -> None:
            async with McpHub(live_targets()) as hub:
                await hub.read_resource("live", FEED)
                assert ("live", FEED) in hub.cache.keys()
                await hub.call_tool("retire_feed")
                await hub.wait_for(lambda: not any(r.uri == FEED for s in hub.snapshots for r in s.resources))
                assert ("live", FEED) not in hub.cache.keys()
                with pytest.raises(HubError):
                    await hub.read_resource("live", FEED)
                await hub.wait_for(lambda: hub.is_live("live"))
                assert hub.is_live("live")  # re-subscribed with the new resource list
                assert any(e.startswith("stale live") for e in hub.events)

        run(main)

    def test_updates_keep_working_after_a_resubscribe(self) -> None:
        async def main() -> None:
            async with McpHub(live_targets()) as hub:
                await hub.call_tool("enable_annotate")
                await hub.wait_for(lambda: "annotate" in hub.negotiate().routes)
                await hub.read_resource("live", FEED)
                await hub.call_tool("post_update", {"text": "again"})
                await hub.wait_for(lambda: ("live", FEED) not in hub.cache.keys())

        run(main)


# --- failure injection with a fake client ---


class FakeClient:
    """Just enough client surface for the hub; ``script`` drives each successive listen() stream."""

    def __init__(
        self,
        script: list[list[Any]],
        *,
        version: str = LISTEN_MIN_VERSION,
        uris: tuple[str, ...] = ("fake://a",),
        later_uris: tuple[str, ...] | None = None,
    ) -> None:
        self.script = [list(s) for s in script]
        self.protocol_version = version
        self.uris = list(uris)
        self.later_uris = list(later_uris) if later_uris is not None else None
        self.list_calls = 0
        self.listen_calls: list[dict[str, Any]] = []
        self.entered = self.exited = 0
        self.server_capabilities = SimpleNamespace(resources=SimpleNamespace())
        self.server_info = SimpleNamespace(name="fake")
        self.instructions = None
        self.reads = 0

    async def __aenter__(self) -> FakeClient:
        self.entered += 1
        return self

    async def __aexit__(self, *a: object) -> None:
        self.exited += 1

    async def list_tools(self) -> Any:
        tool = SimpleNamespace(name="t", description="d", input_schema={"type": "object"})
        return SimpleNamespace(tools=[tool])

    async def list_resources(self) -> Any:
        self.list_calls += 1
        uris = self.later_uris if self.later_uris is not None and self.list_calls > 1 else self.uris
        rs = [SimpleNamespace(uri=u, name=u, description=None, mime_type=None) for u in uris]
        return SimpleNamespace(resources=rs)

    async def read_resource(self, uri: str) -> Any:
        self.reads += 1
        return SimpleNamespace(contents=[SimpleNamespace(text=f"v{self.reads}")])

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return SimpleNamespace(content=[SimpleNamespace(text="ok")], is_error=False)

    def listen(self, **kwargs: Any) -> Any:
        self.listen_calls.append(kwargs)
        items = self.script.pop(0) if self.script else ["block"]

        class Sub:
            def __init__(self, items: list[Any]) -> None:
                self.items = items

            def __aiter__(self) -> Sub:
                return self

            async def __anext__(self) -> Any:
                if not self.items or self.items[0] == "block":
                    await anyio.sleep_forever()
                item = self.items.pop(0)
                if item == "lost":
                    raise SubscriptionLost("dropped")
                if item == "end":
                    raise StopAsyncIteration
                return item

        @asynccontextmanager
        async def ctx() -> Any:
            yield Sub(items)

        return ctx()


def fake_hub(fake: FakeClient, **kw: Any) -> McpHub:
    kw.setdefault("relisten_delay", 0.0)
    return McpHub({"f": "target"}, client_factory=lambda _t: fake, **kw)


class TestFailureInjection:
    def test_dropped_stream_is_reopened_and_the_cache_is_dropped(self) -> None:
        fake = FakeClient([["lost"], ["block"]])

        async def main() -> None:
            async with fake_hub(fake) as hub:
                await hub.wait_for(lambda: len(fake.listen_calls) == 2)
                assert hub.relistens == 1
                assert hub.is_live("f")
                assert any("stream lost f (attempt 1)" in e for e in hub.events)

        run(main)

    def test_cache_is_dropped_when_the_stream_is_lost(self) -> None:
        fake = FakeClient([["lost"], ["block"]])

        async def main() -> None:
            hub = fake_hub(fake)
            hub.cache.put(("f", "fake://a"), "stale")
            async with hub:
                await hub.wait_for(lambda: hub.relistens == 1)
                assert ("f", "fake://a") not in hub.cache.keys()

        run(main)

    def test_gives_up_after_the_attempt_limit_and_falls_back_to_ttl(self) -> None:
        fake = FakeClient([["lost"]] * 5)

        async def main() -> None:
            async with fake_hub(fake, relisten_attempts=2) as hub:
                await hub.wait_for(lambda: any(e.startswith("fallback f") for e in hub.events))
                assert hub.relistens == 3  # attempts 1 and 2 retried, 3 exceeded the limit
                assert len(fake.listen_calls) == 3
                assert not hub.is_live("f")
                assert hub.state("f") is ConnState.OPEN  # calls still work
                assert await hub.read_resource("f", "fake://a") == "v1"

        run(main)

    def test_retries_back_off_instead_of_spinning(self) -> None:
        fake = FakeClient([["lost"]] * 5)

        async def main() -> float:
            async with fake_hub(fake, relisten_attempts=2, relisten_delay=0.05) as hub:
                start = anyio.current_time()
                await hub.wait_for(lambda: any(e.startswith("fallback f") for e in hub.events))
                return anyio.current_time() - start

        elapsed = run(main)
        assert elapsed >= 0.12  # 0.05 * 1 + 0.05 * 2 before the third loss gives up

    def test_zero_attempts_means_no_retry(self) -> None:
        fake = FakeClient([["lost"], ["block"]])

        async def main() -> None:
            async with fake_hub(fake, relisten_attempts=0) as hub:
                await hub.wait_for(lambda: any(e.startswith("fallback f") for e in hub.events))
                assert len(fake.listen_calls) == 1

        run(main)

    def test_attempt_counter_resets_after_a_healthy_stream(self) -> None:
        ev = ResourceUpdated(uri="fake://a")
        fake = FakeClient([["lost"], [ev, "lost"], [ev, "lost"], ["block"]])

        async def main() -> None:
            async with fake_hub(fake, relisten_attempts=1) as hub:
                await hub.wait_for(lambda: len(fake.listen_calls) == 4)
                assert hub.is_live("f")
                assert not any(e.startswith("fallback") for e in hub.events)

        run(main)

    def test_graceful_server_close_falls_back_to_ttl(self) -> None:
        fake = FakeClient([["end"]])

        async def main() -> None:
            async with fake_hub(fake) as hub:
                await hub.wait_for(lambda: any("stream closed by f" in e for e in hub.events))
                assert not hub.is_live("f")
                assert len(fake.listen_calls) == 1

        run(main)

    def test_old_protocol_never_listens(self) -> None:
        fake = FakeClient([["block"]], version="2025-11-25")

        async def main() -> None:
            async with fake_hub(fake) as hub:
                await anyio.sleep(0.05)
                assert fake.listen_calls == []
                assert not hub.is_live("f")
                assert hub.negotiate().polling_only == ("f",)
                assert await hub.read_resource("f", "fake://a") == "v1"
                assert await hub.read_resource("f", "fake://a") == "v1"  # cached
                assert fake.reads == 1

        run(main)

    def test_listen_requests_the_right_subscriptions(self) -> None:
        fake = FakeClient([["block"]], uris=("fake://a", "fake://b"))

        async def main() -> None:
            async with fake_hub(fake) as hub:
                await hub.wait_for(lambda: len(fake.listen_calls) == 1)

        run(main)
        kw = fake.listen_calls[0]
        assert kw["resource_subscriptions"] == ["fake://a", "fake://b"]
        assert kw["tools_list_changed"] is True and kw["resources_list_changed"] is True

    def test_events_from_a_scripted_stream(self) -> None:
        fake = FakeClient([[ResourceUpdated(uri="fake://a"), ToolsListChanged(), "block"]])

        async def main() -> None:
            hub = fake_hub(fake)
            hub.cache.put(("f", "fake://a"), "old")
            async with hub:
                await hub.wait_for(lambda: any(e.startswith("tools changed") for e in hub.events))
                assert ("f", "fake://a") not in hub.cache.keys()
                assert any(e.startswith("updated f: fake://a (cache entry dropped)") for e in hub.events)

        run(main)

    def test_resources_changed_resubscribes_with_the_new_list(self) -> None:
        fake = FakeClient(
            [[ResourcesListChanged()], ["block"]], uris=("fake://a",), later_uris=("fake://a", "fake://c")
        )

        async def main() -> None:
            async with fake_hub(fake) as hub:
                await hub.wait_for(lambda: len(fake.listen_calls) == 2)

        run(main)
        assert fake.listen_calls[1]["resource_subscriptions"] == ["fake://a", "fake://c"]

    def test_crashing_call_surfaces_as_unavailable(self) -> None:
        fake = FakeClient([["block"]])

        async def boom(name: str, args: dict[str, Any]) -> Any:
            raise OSError("pipe closed")

        fake.call_tool = boom  # type: ignore[method-assign]

        async def main() -> None:
            async with fake_hub(fake) as hub:
                with pytest.raises(ServerUnavailable):
                    await hub.call_tool("t")

        run(main)

    def test_context_manager_of_every_client_is_exited(self) -> None:
        fake = FakeClient([["lost"], ["block"]])

        async def main() -> None:
            async with fake_hub(fake) as hub:
                await hub.wait_for(lambda: len(fake.listen_calls) == 2)

        run(main)
        assert fake.entered == fake.exited == 1


# --- a real stdio round trip ---


class TestStdio:
    def test_discovery_over_a_real_subprocess(self) -> None:
        async def main() -> None:
            targets = {"logs": _stdio_params("logs", INC), "deploys": _stdio_params("deploys", INC)}
            async with McpHub(targets, watch=False) as hub:
                assert hub.open_servers == ["logs", "deploys"]
                routes = hub.negotiate().routes
                assert routes["get_incident_meta"] == "logs" and routes["get_recent_deploys"] == "deploys"
                text, err = await hub.call_tool("get_recent_deploys")
                assert not err and text
                res = await hub.read_resource("logs", resource_uri(INC, "meta"))
                assert INC in res
            assert hub.opened == hub.closed == 2

        run(main)
