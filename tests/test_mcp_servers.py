"""Tests for the Stage 13 MCP servers, the sandbox diagnostics and the McpToolbox facade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed (pip install -e .[mcp])")

import anyio  # noqa: E402
from mcp.client import Client  # noqa: E402

from incident_agent.mcp_servers import backend as backend_mod  # noqa: E402
from incident_agent.mcp_servers import diagnostics as diag  # noqa: E402
from incident_agent.mcp_servers.backend import BACKEND_TOOLS, EXTRA_TOOLS, LEGACY_TOOLS, IncidentBackend  # noqa: E402
from incident_agent.mcp_servers.client import EXTENDED_TOOLS, McpToolbox  # noqa: E402
from incident_agent.mcp_servers.servers import SERVER_KINDS, SERVER_TOOLS, build_server  # noqa: E402
from incident_agent.tools.toolbox import TOOL_NAMES, TOOL_SPECS, IncidentToolbox, ToolError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ALL_IDS = [f"INC-{i:03d}" for i in range(1, 29)]
START = "2026-03-01T00:40:00+00:00"
END = "2026-03-01T00:41:00+00:00"


def err(fn, *a, **k) -> ToolError:
    with pytest.raises(ToolError) as info:
        fn(*a, **k)
    return info.value


# --- backend ----------------------------------------------------------------------------------------------------


class TestBackend:
    b = IncidentBackend("INC-001")

    def test_tool_inventory(self):
        assert LEGACY_TOOLS == tuple(TOOL_NAMES) and set(BACKEND_TOOLS) == set(LEGACY_TOOLS) | set(EXTRA_TOOLS)

    def test_legacy_tools_are_delegated_unchanged(self):
        direct = IncidentToolbox("INC-001")
        for name in LEGACY_TOOLS:
            args = {"query": "cpu"} if name == "search_logs" else {}
            assert self.b.call(name, args) == direct.call(name, args)

    @pytest.mark.parametrize("bad", ["INC-1", "../x", "INC-999", ""])
    def test_bad_incident_ids(self, bad):
        with pytest.raises(ValueError):
            IncidentBackend(bad)

    def test_unknown_tool_and_non_dict_arguments(self):
        assert "unknown tool" in err(self.b.call, "nope", {}).reason
        e = err(self.b.call, "log_window", ["x"])
        assert e.bad_args and "JSON object" in e.reason

    def test_log_window_filters_by_time_and_level(self):
        text = self.b.call("log_window", {"start": START, "end": END, "level": "error"})
        lines = text.splitlines()[1:]
        assert lines and all(START[:16] <= ln[:16] <= END[:16] and " ERROR " in ln for ln in lines)
        assert text.startswith(f"{len(lines)} lines (showing {len(lines)})")

    def test_log_window_limit_and_total_count(self):
        text = self.b.call("log_window", {"limit": 3})
        head, *lines = text.splitlines()
        assert len(lines) == 3 and head.startswith("215 lines (showing 3)")

    def test_log_window_empty(self):
        text = self.b.call("log_window", {"start": "2030-01-01T00:00:00+00:00"})
        assert text.startswith("no log lines between 2030-01-01")

    @pytest.mark.parametrize(
        "args", [{"start": "yesterday"}, {"end": "13:00"}, {"limit": 0}, {"limit": 51}, {"level": "LOUD"}, {"x": 1}]
    )
    def test_log_window_bad_arguments(self, args):
        e = err(self.b.call, "log_window", args)
        assert e.bad_args and "bad arguments" in e.reason

    def test_list_metrics(self):
        text = self.b.call("list_metrics", {})
        assert "cpu_percent: 90 points 2026-03-01T00:00:00+00:00 .. 2026-03-01T01:29:00+00:00" in text
        assert err(self.b.call, "list_metrics", {"x": 1}).bad_args

    def test_query_metric_downsamples_and_keeps_the_peak(self):
        text = self.b.call("query_metric", {"metric": "cpu_percent", "max_points": 5})
        head, *rows = text.splitlines()
        assert head.startswith("cpu_percent: 90 points, showing 6 (peak 103.1@2026-03-01T01:12:00+00:00)")
        assert rows[0].startswith("2026-03-01T00:00:00") and rows[-1].startswith("2026-03-01T01:29:00")
        assert any(r.endswith("103.1") for r in rows) and rows == sorted(rows)

    def test_query_metric_short_series_is_returned_in_full(self):
        text = self.b.call(
            "query_metric", {"metric": "cpu_percent", "start": START, "end": "2026-03-01T00:44:00+00:00"}
        )
        assert text.splitlines()[0].startswith("cpu_percent: 5 points, showing 5")

    def test_query_metric_window_without_points(self):
        assert "no points between" in self.b.call(
            "query_metric", {"metric": "cpu_percent", "start": "2030-01-01T00:00:00"}
        )

    def test_timestamps_without_an_offset_are_read_as_utc(self):
        naive = self.b.call("log_window", {"start": "2026-03-01T00:40:00", "end": "2026-03-01T00:41:00"})
        aware = self.b.call("log_window", {"start": START, "end": END})
        assert naive == aware and not naive.startswith("no log lines")
        assert self.b.call("query_metric", {"metric": "cpu_percent", "end": "2026-03-01T00:02:00"}).startswith(
            "cpu_percent: 3 points"
        )

    def test_query_metric_unknown_metric_lists_the_available_ones(self):
        e = err(self.b.call, "query_metric", {"metric": "nope"})
        assert e.bad_args and "cpu_percent" in e.reason and "latency_p99_ms" in e.reason

    @pytest.mark.parametrize(
        "args", [{}, {"metric": ""}, {"metric": "cpu_percent", "max_points": 2}, {"metric": "m", "start": "x"}]
    )
    def test_query_metric_bad_arguments(self, args):
        assert err(self.b.call, "query_metric", args).bad_args

    def test_missing_data_files_are_reported_as_unavailable(self, monkeypatch):
        def boom(_):
            raise FileNotFoundError("metrics.json")

        monkeypatch.setattr(backend_mod, "read_metrics", boom)
        e = err(self.b.call, "list_metrics", {})
        assert not e.bad_args and "data source unavailable" in e.reason

    def test_output_is_clipped(self, monkeypatch):
        monkeypatch.setattr(backend_mod, "_clip", lambda t: t[:10])
        assert len(self.b.call("list_metrics", {})) == 10


# --- diagnostics -----------------------------------------------------------------------------------------------


class TestDiagnostics:
    def test_registry_and_description(self):
        text = diag.describe_scripts()
        assert set(diag.SCRIPTS) == {"error_timeline", "first_seen", "metric_vs_errors", "service_breakdown"}
        assert "first_seen (pattern: str)" in text and "service_breakdown (no arguments)" in text

    def test_service_breakdown(self):
        assert diag.run_diagnostic("INC-001", "service_breakdown") == "checkout-api: 125 (100% of 125 ERROR/WARN lines)"

    def test_first_seen(self):
        out = diag.run_diagnostic("INC-001", "first_seen", {"pattern": "THROTTLING"})
        assert out.startswith("37 lines contain 'THROTTLING'; first 2026-03-01T00:40:20+00:00 [checkout-api] ERROR")
        assert diag.run_diagnostic("INC-001", "first_seen", {"pattern": "zzzz"}) == "no log lines contain 'zzzz'"

    def test_error_timeline(self):
        out = diag.run_diagnostic("INC-001", "error_timeline", {"bucket_minutes": 30})
        assert out.splitlines()[0] == "125 lines from 2026-03-01T00:40:20+00:00, 30-minute buckets:"
        assert out.splitlines()[1:] == ["+  0m checkout-api: 71", "+ 30m checkout-api: 54"]
        assert "no log lines" in diag.run_diagnostic("INC-001", "error_timeline", {"levels": ["FATAL"]})

    def test_metric_vs_errors(self):
        out = diag.run_diagnostic("INC-001", "metric_vs_errors", {"metric": "cpu_percent"})
        assert "anomaly_start=2026-03-01T00:42:00+00:00 first_error=2026-03-01T00:40:20+00:00" in out
        assert out.endswith("first ERROR is 2 min before the metric anomaly")

    def test_unknown_metric_is_a_bad_arguments_error_from_the_child(self):
        e = err(diag.run_diagnostic, "INC-001", "metric_vs_errors", {"metric": "nope"})
        assert e.bad_args and "unknown metric" in e.reason

    def test_unknown_script_and_bad_arguments_never_start_a_process(self, monkeypatch):
        monkeypatch.setattr(diag.subprocess, "run", lambda *a, **k: pytest.fail("a process was started"))
        assert "available: error_timeline" in err(diag.run_diagnostic, "INC-001", "rm_rf").reason
        for name, args in [
            ("first_seen", {}),
            ("first_seen", {"pattern": "a", "extra": 1}),
            ("error_timeline", {"bucket_minutes": 0}),
            ("service_breakdown", {"x": 1}),
        ]:
            assert err(diag.run_diagnostic, "INC-001", name, args).bad_args
        assert err(diag.run_diagnostic, "INC-001", "first_seen", ["a"]).bad_args  # type: ignore[arg-type]

    def test_timeout_kills_the_process(self):
        e = err(diag.run_diagnostic, "INC-001", "service_breakdown", timeout=0.001)
        assert not e.bad_args and "exceeded the 0.001s limit" in e.reason

    def test_output_cap(self):
        out = diag.run_diagnostic("INC-001", "error_timeline", {"bucket_minutes": 1}, max_output=40)
        assert out.endswith("\n...[truncated]") and len(out) == 40 + len("\n...[truncated]")

    def test_unknown_incident_and_invalid_id_are_errors_not_crashes(self):
        assert "FileNotFoundError" in err(diag.run_diagnostic, "INC-999", "service_breakdown").reason
        assert "invalid incident id" in err(diag.run_diagnostic, "../etc", "service_breakdown").reason

    def test_child_environment_carries_no_secrets(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "secret")
        monkeypatch.setenv("MISTRAL_API_KEY", "secret")
        monkeypatch.setenv("PATH", "/bin")
        child_env = diag._child_env()
        assert child_env["PATH"] == "/bin"
        assert set(child_env) <= {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG"}
        assert "GROQ_API_KEY" not in child_env and "MISTRAL_API_KEY" not in child_env

    def test_process_start_failure(self, monkeypatch):
        def fail(*a, **k):
            raise OSError("no fork")

        monkeypatch.setattr(diag.subprocess, "run", fail)
        assert (
            "could not start the sandbox process: no fork"
            in err(diag.run_diagnostic, "INC-001", "service_breakdown").reason
        )

    def test_garbage_from_the_child_is_reported(self, monkeypatch):
        class P:
            stdout, stderr, returncode = "not json", "Traceback ... boom", 1

        monkeypatch.setattr(diag.subprocess, "run", lambda *a, **k: P())
        assert "failed (exit 1): Traceback ... boom" in err(diag.run_diagnostic, "INC-001", "service_breakdown").reason

    def test_child_reported_failure_flags(self, monkeypatch):
        class P:
            returncode, stderr = 0, ""
            stdout = json.dumps({"ok": False, "error": "nope", "bad_args": True})

        monkeypatch.setattr(diag.subprocess, "run", lambda *a, **k: P())
        e = err(diag.run_diagnostic, "INC-001", "service_breakdown")
        assert (e.reason, e.bad_args) == ("nope", True)

    def test_scripts_only_read_the_scoped_incident(self):
        assert "INC-002" not in diag.run_diagnostic("INC-001", "error_timeline")


# --- servers ----------------------------------------------------------------------------------------------------


def with_client(kind: str, incident: str, coro, **kw):
    async def main():
        async with Client(build_server(kind, incident, **kw)) as c:
            return await coro(c)

    return anyio.run(main)


class TestServers:
    def test_each_server_exposes_exactly_its_tools(self):
        for kind in SERVER_KINDS:
            names = with_client(kind, "INC-001", lambda c: _names(c))
            assert names == sorted(SERVER_TOOLS[kind])
        assert {t for tools in SERVER_TOOLS.values() for t in tools} == set(BACKEND_TOOLS) | {
            "list_diagnostics",
            "run_diagnostic",
        }

    def test_incident_is_not_a_tool_argument(self):
        for kind in SERVER_KINDS:
            tools = with_client(kind, "INC-001", lambda c: _tools(c))
            for t in tools:
                assert not any("incident" in k.lower() for k in t.input_schema.get("properties", {})), t.name

    def test_build_errors(self):
        with pytest.raises(ValueError, match="unknown server"):
            build_server("pager", "INC-001")
        with pytest.raises(ValueError, match="unknown incident"):
            build_server("logs", "INC-999")

    def test_call_and_error_tags(self):
        async def run(c):
            ok = await c.call_tool("search_logs", {"query": "throttling", "limit": 1})
            bad = await c.call_tool("search_logs", {"query": ""})
            typed = await c.call_tool("search_logs", {"query": "x", "limit": "many"})
            unknown = await c.call_tool("nope", {})
            return ok, bad, typed, unknown

        ok, bad, typed, unknown = with_client("logs", "INC-001", run)
        assert not ok.is_error and "matches (showing 1)" in ok.content[0].text
        assert bad.is_error and "[bad_args] bad arguments (query:" in bad.content[0].text
        assert typed.is_error and "[bad_args]" not in typed.content[0].text  # rejected by the MCP layer itself
        assert unknown.is_error and "Unknown tool" in unknown.content[0].text

    def test_unavailable_tag(self, monkeypatch):
        def boom(_):
            raise FileNotFoundError("logs.log")

        monkeypatch.setattr("incident_agent.tools.toolbox.read_logs", boom)
        res = with_client("logs", "INC-001", lambda c: c.call_tool("log_overview", {}))
        assert res.is_error and "[unavailable] data source unavailable" in res.content[0].text

    def test_sandbox_server(self):
        async def run(c):
            lst = await c.call_tool("list_diagnostics", {})
            ok = await c.call_tool("run_diagnostic", {"script": "service_breakdown"})
            bad = await c.call_tool("run_diagnostic", {"script": "bash", "arguments": {"cmd": "id"}})
            return lst, ok, bad

        lst, ok, bad = with_client("sandbox", "INC-001", run)
        assert "first_seen (pattern: str)" in lst.content[0].text
        assert ok.content[0].text.startswith("checkout-api: 125")
        assert bad.is_error and "[bad_args] unknown script 'bash'" in bad.content[0].text

    def test_sandbox_timeout_is_unavailable_not_bad_args(self):
        res = with_client(
            "sandbox",
            "INC-001",
            lambda c: c.call_tool("run_diagnostic", {"script": "service_breakdown"}),
            diagnostic_timeout=0.001,
        )
        assert res.is_error and "[unavailable]" in res.content[0].text

    def test_tool_descriptions_match_the_agent_specs(self):
        wanted = {t["function"]["name"]: t["function"]["description"] for t in TOOL_SPECS}
        for kind in SERVER_KINDS:
            for t in with_client(kind, "INC-001", lambda c: _tools(c)):
                if t.name in wanted:
                    assert t.description == wanted[t.name]

    def test_unknown_extra_arguments_are_ignored_by_the_mcp_layer(self):
        res = with_client("logs", "INC-001", lambda c: c.call_tool("search_logs", {"query": "throttling", "bogus": 1}))
        assert not res.is_error  # documented difference from IncidentToolbox, which rejects them

    def test_stdio_entry_point_rejects_bad_incident(self, capsys):
        from incident_agent.mcp_servers.__main__ import main

        assert main(["logs", "--incident", "INC-999"]) == 2
        assert capsys.readouterr().out.startswith("ERROR: unknown incident")


async def _tools(c):
    return (await c.list_tools()).tools


async def _names(c):
    return sorted(t.name for t in await _tools(c))


# --- McpToolbox -------------------------------------------------------------------------------------------------

SEARCH_VARIANTS = [
    {"query": "cpu"},
    {"query": "THROTTLING", "level": "error"},
    {"query": "a", "limit": 1000},
    {"query": "a", "limit": 0},
    {"query": "a", "level": None, "limit": None},
    {"query": "zzzzzz"},
    {"query": "a", "level": "LOUD"},
    {"query": ""},
    {"limit": 3},
    {"query": "a", "limit": "3"},
]


def outcome(box, name, args):
    try:
        return ("ok", box.call(name, args))
    except ToolError as exc:
        return ("err", exc.tool, exc.reason, exc.bad_args)


class TestMcpToolboxParity:
    @pytest.mark.parametrize("incident", ALL_IDS)
    def test_original_tools_return_identical_text_for_every_incident(self, incident):
        direct = IncidentToolbox(incident)
        with McpToolbox(incident) as mcp_box:
            for name in TOOL_NAMES:
                args = {"query": "error"} if name == "search_logs" else {}
                assert outcome(mcp_box, name, args) == outcome(direct, name, args), (incident, name)

    def test_search_logs_argument_variants_behave_the_same(self):
        direct = IncidentToolbox("INC-001")
        with McpToolbox("INC-001") as box:
            for args in SEARCH_VARIANTS:
                got, want = outcome(box, "search_logs", args), outcome(direct, "search_logs", args)
                if args == {"limit": 3}:  # a missing required argument is rejected by the MCP layer: wording differs
                    assert got[:2] == want[:2] and got[3] is want[3] is True
                else:
                    assert got == want, args

    def test_caller_mistakes_are_reported_the_same_way(self):
        direct = IncidentToolbox("INC-001", fail_tools={"log_overview"}, allowed_tools={"search_logs", "log_overview"})
        with McpToolbox("INC-001", fail_tools={"log_overview"}, allowed_tools={"search_logs", "log_overview"}) as box:
            for name, args in [
                ("no_such_tool", {}),
                ("get_metric_summary", {}),  # not allowed
                ("log_overview", {}),  # injected failure
                ("search_logs", "text"),
                ("search_logs", {"_raw": "{bad"}),
            ]:
                assert outcome(box, name, args) == outcome(direct, name, args), (name, args)

    def test_no_gold_text_reaches_the_model(self):
        gold = json.loads((ROOT / "eval" / "gold" / "INC-001.json").read_text())
        words = gold["root_cause"].lower().split()
        shingles = {" ".join(words[i : i + 6]) for i in range(len(words) - 5)}
        with McpToolbox("INC-001", extended=True) as box:
            for name in box.tool_names:
                args = (
                    {"query": "error"}
                    if name == "search_logs"
                    else {"metric": "cpu_percent"}
                    if name == "query_metric"
                    else {"script": "service_breakdown"}
                    if name == "run_diagnostic"
                    else {}
                )
                text = box.call(name, args).lower()
                assert not any(s in text for s in shingles), name


class TestMcpToolbox:
    def test_interface_defaults(self):
        box = McpToolbox("INC-001")
        assert box.incident_id == "INC-001" and box.allowed == set(TOOL_NAMES) and box.tool_names == TOOL_NAMES

    def test_specs_for_original_tools_are_the_unchanged_agent_specs(self):
        assert McpToolbox("INC-001").specs() == TOOL_SPECS
        assert McpToolbox("INC-001", allowed_tools={"search_logs"}).specs() == [
            t for t in TOOL_SPECS if t["function"]["name"] == "search_logs"
        ]

    def test_opening_is_lazy_and_close_is_idempotent(self):
        box = McpToolbox("INC-001")
        assert box._stack is None
        box.specs()
        assert box._stack is None  # originals need no server
        box.call("log_overview", {})
        assert box._stack is not None and set(box._clients) == {"logs", "metrics", "deploys"}
        box.close()
        box.close()
        assert box._stack is None and not box._clients
        assert box.call("get_incident_meta", {}).startswith("{")  # reopens on demand
        box.close()

    def test_context_manager_closes(self):
        with McpToolbox("INC-001") as box:
            box.call("log_overview", {})
        assert box._stack is None

    def test_extended_tools_and_specs(self):
        with McpToolbox("INC-001", extended=True) as box:
            assert box.tool_names == [*TOOL_NAMES, *EXTENDED_TOOLS]
            names = [s["function"]["name"] for s in box.specs()]
            assert names == [*TOOL_NAMES, *EXTENDED_TOOLS]
            qm = next(s for s in box.specs() if s["function"]["name"] == "query_metric")["function"]
            assert qm["parameters"]["required"] == ["metric"] and "title" not in qm["parameters"]
            assert "title" not in qm["parameters"]["properties"]["metric"]
            assert box.call("list_diagnostics", {}).count("\n") == 3
            assert box.call("run_diagnostic", {"script": "first_seen", "arguments": {"pattern": "throttl"}}).startswith(
                "37 lines"
            )

    def test_extended_errors_map_to_tool_errors(self):
        with McpToolbox("INC-001", extended=True) as box:
            e = err(box.call, "query_metric", {"metric": "nope"})
            assert e.bad_args and "unknown metric" in e.reason
            e = err(box.call, "run_diagnostic", {"script": "bash"})
            assert e.bad_args and "unknown script" in e.reason
        with McpToolbox("INC-001", extended=True, diagnostic_timeout=0.001) as box:
            e = err(box.call, "run_diagnostic", {"script": "service_breakdown"})
            assert not e.bad_args and "exceeded" in e.reason

    def test_extended_tools_are_not_visible_by_default(self):
        with McpToolbox("INC-001") as box:
            e = err(box.call, "query_metric", {"metric": "cpu_percent"})
            assert "unknown tool" in e.reason and "query_metric" not in e.reason

    def test_transport_failure_is_unavailable_not_bad_args(self):
        box = McpToolbox("INC-001")
        box.call("log_overview", {})

        async def explode(*a, **k):
            raise RuntimeError("connection closed")

        box._clients["logs"].call_tool = explode  # type: ignore[method-assign]
        e = err(box.call, "log_overview", {})
        assert not e.bad_args and "data source unavailable: RuntimeError: connection closed" in e.reason
        box.close()

    @pytest.mark.parametrize("bad", ["INC-1", "../x", "INC-999"])
    def test_bad_incident(self, bad):
        with pytest.raises(ValueError):
            McpToolbox(bad)

    def test_bad_transport(self):
        with pytest.raises(ValueError, match="transport"):
            McpToolbox("INC-001", transport="carrier-pigeon")

    def test_error_text_mapping(self):
        m = McpToolbox._to_tool_error
        e = m("t", "Error executing tool t: [bad_args] fix it")
        assert (e.reason, e.bad_args) == ("fix it", True)
        e = m("t", "Error executing tool t: [unavailable] down\nsecond line")
        assert (e.reason, e.bad_args) == ("down\nsecond line", False)
        e = m("t", "Error executing tool t: 1 validation error for tArguments")
        assert e.bad_args and e.reason == "bad arguments (1 validation error for tArguments)"
        assert m("t", "Unknown tool: t").reason == "Unknown tool: t"
        e = m("t", "Error executing tool t")
        assert not e.bad_args and e.reason.startswith("internal error")

    def test_stdio_transport_serves_the_same_answers(self):
        direct = IncidentToolbox("INC-001")
        with McpToolbox("INC-001", transport="stdio", extended=True) as box:
            assert box.call("log_overview", {}) == direct.call("log_overview", {})
            assert box.call("get_recent_deploys", {}) == direct.call("get_recent_deploys", {})
            assert box.call("run_diagnostic", {"script": "service_breakdown"}).startswith("checkout-api: 125")
            e = err(box.call, "search_logs", {"query": ""})
            assert e.bad_args and "bad arguments" in e.reason

    def test_failed_start_cleans_up(self, monkeypatch):
        box = McpToolbox("INC-001")
        monkeypatch.setattr(
            "incident_agent.mcp_servers.client.build_server",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError, match="boom"):
            box.call("log_overview", {})
        assert box._stack is None and not box._clients


# --- the script bodies, in-process (they normally run in the child process, where coverage cannot see them) -------


class TestScriptBodies:
    def run(self, name, iid="INC-001", **args):
        model, fn, _ = diag.SCRIPTS[name]
        return fn(iid, model.model_validate(args))

    def test_bodies_agree_with_the_subprocess_results(self):
        for name, args in [
            ("service_breakdown", {}),
            ("first_seen", {"pattern": "throttling"}),
            ("error_timeline", {"bucket_minutes": 30}),
            ("metric_vs_errors", {"metric": "cpu_percent"}),
        ]:
            assert self.run(name, **args) == diag.run_diagnostic("INC-001", name, args)

    def test_no_errors_or_warnings(self):
        assert self.run("service_breakdown", "INC-019") == "no ERROR or WARN lines"

    def test_metric_without_errors_to_compare(self):
        out = self.run("metric_vs_errors", "INC-023", metric="latency_p99_ms")
        assert "first_error=None" in out and out.endswith("no anomaly or no ERROR lines to compare")

    def test_metric_that_moves_before_the_first_error(self):
        out = self.run("metric_vs_errors", "INC-001", metric="latency_p99_ms")
        assert "min before the metric anomaly" in out or "min after the metric anomaly" in out

    def test_first_seen_counts_every_match(self):
        out = self.run("first_seen", pattern="error")
        assert out.startswith("first") is False and "lines contain 'error'" in out

    def test_error_timeline_levels_and_bucket_labels(self):
        out = self.run("error_timeline", "INC-022", bucket_minutes=10, levels=["warn"])
        assert "10-minute buckets" in out and "search-indexer" in out

    def test_unknown_metric_raises_a_bad_arguments_tool_error(self):
        e = err(self.run, "metric_vs_errors", metric="nope")
        assert e.bad_args

    def test_empty_metrics_store(self, monkeypatch):
        monkeypatch.setattr(backend_mod, "read_metrics", lambda _: {})
        assert "metrics store is empty" in err(IncidentBackend("INC-001").call, "list_metrics", {}).reason


class TestSandboxProcessContract:
    def test_the_child_runs_in_isolated_mode_with_a_scrubbed_environment(self, monkeypatch):
        seen = {}

        class P:
            returncode, stderr = 0, ""
            stdout = json.dumps({"ok": True, "output": "fine"})

        def fake_run(argv, **kw):
            seen.update(argv=argv, **kw)
            return P()

        monkeypatch.setenv("GROQ_API_KEY", "secret")
        monkeypatch.setattr(diag.subprocess, "run", fake_run)
        assert diag.run_diagnostic("INC-001", "service_breakdown") == "fine"
        assert seen["argv"][1] == "-I" and "-c" in seen["argv"]
        assert "GROQ_API_KEY" not in seen["env"] and seen["timeout"] == diag.DEFAULT_TIMEOUT_S
        assert json.loads(seen["input"]) == {"incident_id": "INC-001", "name": "service_breakdown", "arguments": {}}

    def test_metric_and_first_error_at_the_same_minute(self, monkeypatch):
        from incident_agent.tools import LogEntry

        ts = "2026-03-01T00:10:00+00:00"
        series = [{"ts": f"2026-03-01T00:0{i}:00+00:00", "value": 1.0} for i in range(5)] + [{"ts": ts, "value": 50.0}]
        monkeypatch.setattr(diag, "read_metrics", lambda _: {"m": series})
        monkeypatch.setattr(diag, "read_logs", lambda _: [LogEntry(ts, "svc", "ERROR", "boom")])
        out = diag.metric_vs_errors("INC-001", diag._MetricArgs(metric="m"))
        assert out.endswith("first ERROR is 0 min after the metric anomaly")
