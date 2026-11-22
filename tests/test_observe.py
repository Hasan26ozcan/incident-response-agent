"""Stage 21: tracing, redaction, pricing, traced client, cascade, window, journal traces, prefix, report, gate."""

from __future__ import annotations

import json
import types
from typing import Any

import pytest
from pydantic import BaseModel

from incident_agent.agenteval import suite
from incident_agent.llm.client import LLMConfigError, LLMResponse, LLMUnavailable, OpenAICompatClient
from incident_agent.llm.scripted import ScriptedClient
from incident_agent.observe import prefix as pfx
from incident_agent.observe import report as rep
from incident_agent.observe import selfcheck as sc
from incident_agent.observe.__main__ import main as cli
from incident_agent.observe.cascade import CascadeClient, default_accept
from incident_agent.observe.exporters import ExporterUnavailable, LangSmithSink, OtelSink, log_wandb
from incident_agent.observe.journal_traces import spans_from_events, spans_from_root
from incident_agent.observe.llm import TracedClient, prefix_hash
from incident_agent.observe.pricing import Price, PriceTable
from incident_agent.observe.redact import MARK, clean_value, redact
from incident_agent.observe.trace import JsonlSink, MemorySink, Span, Tracer, current_span, read_spans
from incident_agent.observe.window import SlidingWindow, WindowedClient, estimate_tokens

KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"


def tick() -> Any:
    return sc._Tick()  # noqa: SLF001


class TestRedact:
    @pytest.mark.parametrize(
        "text",
        [
            KEY,
            "xoxb-1234567890-abcdefghij",
            "ghp_" + "a" * 24,
            "AKIA" + "A" * 16,
            "gsk_" + "a" * 20,
            "Bearer " + "a" * 24,
            "https://hooks.slack.com/services/T0/B0/xyz",
            "eyJabcdefgh.eyJabcdefgh.abcdefghi",
        ],
    )
    def test_credential_shapes(self, text):
        out = redact(f"before {text} after")
        assert MARK in out and text not in out

    @pytest.mark.parametrize(
        "text,keep",
        [("api_key=hunter2", "api_key="), ("password: 'p w'", "password: "), ('token = "abc def"', "token = ")],
    )
    def test_key_value(self, text, keep):
        out = redact(text)
        assert (
            out.startswith(keep) and MARK in out and "hunter2" not in out and "p w" not in out and "abc def" not in out
        )

    def test_plain_text_untouched(self):
        assert redact("disk usage 91% on node-3") == "disk usage 91% on node-3"

    def test_clean_value_types_and_bounds(self):
        assert (
            clean_value(None) is None and clean_value(True) is True and clean_value(3) == 3 and clean_value(1.5) == 1.5
        )
        assert clean_value("x" * 5000).endswith("[truncated]") and len(clean_value("x" * 5000)) < 2100
        assert clean_value([KEY]) == [MARK]
        assert clean_value({"a": {"b": KEY}}) == {"a": {"b": MARK}}
        assert len(clean_value(list(range(500)))) == 50
        assert len(clean_value({str(i): i for i in range(500)})) == 50
        assert clean_value({"k" * 100: 1}).keys() == {"k" * 64}
        assert isinstance(clean_value(object()), str)
        deep = clean_value([[[KEY]]])  # beyond depth 2 it is stringified AND redacted
        assert KEY not in json.dumps(deep)


class TestTracer:
    def test_nesting_status_duration(self):
        m = MemorySink()
        t = Tracer([m], clock=tick())
        with t.span("run", "run") as root:
            assert current_span() is root
            with t.span("a", "llm"):
                pass
            with pytest.raises(ValueError):
                with t.span("b", "tool"):
                    raise ValueError("boom " + KEY)
        assert current_span() is None
        by = {s.name: s for s in m.spans}
        assert by["a"].parent_id == by["run"].span_id and by["a"].trace_id == by["run"].trace_id
        assert by["b"].status == "error" and KEY not in (by["b"].error or "")
        assert by["run"].status == "ok" and by["run"].duration > by["a"].duration > 0

    def test_new_root_new_trace(self):
        m = MemorySink()
        t = Tracer([m])
        with t.span("x"):
            pass
        with t.span("y"):
            pass
        assert m.spans[0].trace_id != m.spans[1].trace_id

    def test_unknown_kind_and_long_name(self):
        m = MemorySink()
        with Tracer([m]).span("n" * 500, "weird") as s:
            pass
        assert s.kind == "span" and len(s.name) == 128

    def test_failing_sink_never_breaks_the_run(self):
        class Boom:
            def emit(self, span):
                raise OSError("full")

        m = MemorySink()
        t = Tracer([Boom()])
        t.add_sink(m)
        with t.span("x"):
            pass
        assert t.dropped == 1 and len(m.spans) == 1

    def test_event_is_a_span(self):
        m = MemorySink()
        sp = Tracer([m], clock=tick()).event("e", "gate", a=1)
        assert sp.attrs["a"] == 1 and m.spans[0].name == "e"

    def test_jsonl_roundtrip_and_torn_lines(self, tmp_path):
        f = tmp_path / "d" / "t.jsonl"
        t = Tracer([JsonlSink(f, fsync=True)], clock=tick())
        with t.span("a", "llm", k=KEY):
            pass
        f.write_text(f.read_text() + '{torn\n\n[1]\n{"trace_id": 1}\n', encoding="utf-8")
        spans = read_spans(f)
        assert len(spans) == 1 and spans[0].attrs["k"] == MARK
        assert read_spans(tmp_path / "none.jsonl") == []
        assert Span.from_dict(spans[0].to_dict()) == spans[0]

    def test_capture_content_default_off(self):
        m = MemorySink()
        t = Tracer([m])
        TracedClient(ScriptedClient([sc._ok()], model="m"), t).chat([{"role": "user", "content": "secret task"}])  # noqa: SLF001
        assert "input" not in m.spans[0].attrs
        m2 = MemorySink()
        t2 = Tracer([m2], capture_content=True)
        TracedClient(ScriptedClient([sc._ok("out " + KEY)], model="m"), t2).chat([{"role": "user", "content": "hi"}])  # noqa: SLF001
        assert m2.spans[0].attrs["input"][0]["content"] == "hi" and KEY not in json.dumps(m2.spans[0].attrs)


class TestPricing:
    def test_load_has_only_verified(self):
        t = PriceTable.load()
        assert set(t.models) == {"mistral-large-latest", "openai/gpt-oss-120b", "openai/gpt-oss-20b"}
        assert t.get("llama-3.3-70b-versatile") is None

    def test_cost_math(self):
        t = PriceTable({"m": Price(1.0, 2.0, 0.5)})
        assert t.cost("m", 1_000_000, 1_000_000) == pytest.approx(3.0)
        assert t.cost("m", 1_000_000, 0, cached_tokens=500_000) == pytest.approx(0.75)
        assert t.cost("m", 100, 0, cached_tokens=10_000) == pytest.approx(100 * 0.5 / 1e6)  # clamped to prompt size
        assert t.cost("m", 100, 0, cached_tokens=-5) == pytest.approx(100 / 1e6)
        assert PriceTable({"m": Price(1.0, 2.0)}).cost("m", 1000, 0, cached_tokens=1000) == pytest.approx(0.001)
        assert t.cost("nope", 1, 1) is None

    def test_summarize_unpriced_is_not_zero(self):
        t = PriceTable({"m": Price(1.0, 1.0)})
        s = t.summarize([{"model": "m", "prompt_tokens": 1_000_000}, {"model": "x", "prompt_tokens": 5}])
        assert s["total_usd"] == 1.0 and s["unpriced_calls"] == 1 and s["by_model"]["x"]["priced"] is False

    def test_custom_file(self, tmp_path):
        f = tmp_path / "p.json"
        f.write_text(json.dumps({"models": {"a": {"input": 1, "output": 2, "cached_input": None}}}))
        assert PriceTable.load(f).get("a") == Price(1.0, 2.0, None)


class TestTracedClient:
    def test_records_usage_cost_and_passes_through(self):
        m = MemorySink()
        inner = ScriptedClient([sc._ok("hi", 1000, 100, 400)], model="openai/gpt-oss-120b")  # noqa: SLF001
        c = TracedClient(inner, Tracer([m], clock=tick()), PriceTable.load())
        r = c.chat([{"role": "user", "content": "q"}], tools=[{"t": 1}], temperature=0.2)
        assert r.content == "hi" and c.model == "openai/gpt-oss-120b"
        a = m.spans[0].attrs
        assert (a["prompt_tokens"], a["completion_tokens"], a["cached_tokens"], a["tools"], a["structured"]) == (
            1000,
            100,
            400,
            1,
            False,
        )
        assert a["cost_usd"] == pytest.approx((600 * 0.15 + 400 * 0.075 + 100 * 0.6) / 1e6)
        assert inner.calls[0]["temperature"] == 0.2

    def test_unpriced_model_cost_none_and_error_span(self):
        m = MemorySink()

        class Down:
            model = "llama"

            def chat(self, *a, **k):
                raise LLMUnavailable("429")

        t = Tracer([m])
        with pytest.raises(LLMUnavailable):
            TracedClient(Down(), t).chat([{"role": "user", "content": "x"}])
        assert m.spans[0].status == "error"
        TracedClient(ScriptedClient([sc._ok()], model="llama"), t).chat([{"role": "user", "content": "x"}])  # noqa: SLF001
        assert m.spans[1].attrs["cost_usd"] is None

    def test_structured_flag(self):
        class S(BaseModel):
            a: int

        m = MemorySink()
        TracedClient(ScriptedClient([sc._ok('{"a":1}')], model="m"), Tracer([m])).chat(
            [{"role": "user", "content": "x"}], response_schema=S
        )  # noqa: SLF001
        assert m.spans[0].attrs["structured"] is True

    def test_prefix_hash_ignores_last_message_only(self):
        a = [{"role": "system", "content": "s"}, {"role": "user", "content": "1"}]
        b = [{"role": "system", "content": "s"}, {"role": "user", "content": "2"}]
        c = [{"role": "system", "content": "t"}, {"role": "user", "content": "1"}]
        assert prefix_hash(a) == prefix_hash(b) != prefix_hash(c)
        assert prefix_hash(a, [1]) != prefix_hash(a, [2])


class TestCachedTokensCapture:
    def _resp(self, details):
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=details)
        msg = types.SimpleNamespace(content="x", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    def _client(self, resp):
        c = OpenAICompatClient.__new__(OpenAICompatClient)
        c.model = "m"
        c._json_object_mode = False  # noqa: SLF001
        c._create = lambda kwargs: resp  # type: ignore[method-assign]  # noqa: SLF001
        return c

    @pytest.mark.parametrize(
        "details,expected",
        [
            (types.SimpleNamespace(cached_tokens=6), {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 6}),
            (types.SimpleNamespace(cached_tokens=0), {"prompt_tokens": 10, "completion_tokens": 2}),
            (None, {"prompt_tokens": 10, "completion_tokens": 2}),
            (types.SimpleNamespace(cached_tokens="6"), {"prompt_tokens": 10, "completion_tokens": 2}),
            (types.SimpleNamespace(cached_tokens=True), {"prompt_tokens": 10, "completion_tokens": 2}),
        ],
    )
    def test_only_a_positive_int_is_recorded(self, details, expected):
        assert self._client(self._resp(details)).chat([{"role": "user", "content": "x"}]).usage == expected


class TestCascade:
    def test_default_accept(self):
        class S(BaseModel):
            a: int

        assert default_accept(LLMResponse(content="x"), None) and not default_accept(LLMResponse(content="  "), None)
        assert default_accept(LLMResponse(tool_calls=[object()]), None)  # type: ignore[list-item]
        assert default_accept(LLMResponse(content='{"a":1}'), S) and not default_accept(LLMResponse(content="nope"), S)
        assert not default_accept(LLMResponse(content=None), S)

    def test_routes_and_stats(self):
        m = MemorySink()
        tr = Tracer([m])
        cheap = ScriptedClient([sc._ok("good"), sc._ok("bad")], model="c")  # noqa: SLF001
        strong = ScriptedClient([sc._ok("strong")], model="s")  # noqa: SLF001
        c = CascadeClient(cheap, strong, lambda r, s: r.content == "good", tr)
        msgs = [{"role": "user", "content": "x"}]
        assert c.chat(msgs).content == "good" and c.chat(msgs).content == "strong"
        assert c.stats == {"cheap_ok": 1, "escalated": 1, "unavailable": 0} and c.escalation_rate() == 0.5
        assert [s.attrs["tier"] for s in m.spans] == ["cheap", "strong"] and m.spans[1].attrs["reason"] == "rejected"

    def test_unavailable_escalates_config_error_does_not(self):
        r = sc.check_cascade()
        assert r["ok"] is True

    def test_zero_decisions(self):
        assert CascadeClient(ScriptedClient([], model="a"), ScriptedClient([], model="b")).escalation_rate() == 0.0

    def test_unavailable_reason_recorded(self):
        class Down:
            model = "d"

            def chat(self, *a, **k):
                raise LLMUnavailable("x")

        m = MemorySink()
        c = CascadeClient(Down(), ScriptedClient([sc._ok()], model="s"), tracer=Tracer([m]))  # noqa: SLF001
        c.chat([{"role": "user", "content": "x"}])
        assert m.spans[0].attrs["reason"] == "unavailable" and c.stats["unavailable"] == 1

    def test_config_error_propagates(self):
        class Bad:
            model = "b"

            def chat(self, *a, **k):
                raise LLMConfigError("401")

        with pytest.raises(LLMConfigError):
            CascadeClient(Bad(), ScriptedClient([sc._ok()], model="s")).chat([{"role": "user", "content": "x"}])  # noqa: SLF001

    def test_saving_is_priced_and_positive(self):
        r = sc.cascade_scenario()
        assert r["escalated"] == 5 and r["saving"] == pytest.approx(0.25)
        assert sc.cascade_scenario(n=20, accept_every=1)["saving"] < 0  # always escalating costs MORE than strong-only


class TestWindow:
    def test_short_history_unchanged(self):
        m = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "t"},
            {"role": "assistant", "content": "a"},
        ]
        assert SlidingWindow(1000).apply(m) == m

    def test_trims_keeps_head_pairs_and_marker(self):
        h = sc.long_history(40)
        out = SlidingWindow(1500).apply(h)
        assert out[:2] == h[:2] and out[-1] == h[-1] and len(out) < len(h)
        assert out[2]["content"].startswith("[context trimmed:")
        assert sc.window_ok(SlidingWindow(1500), 1500)
        no_marker = SlidingWindow(1500, marker=False).apply(h)
        assert len(no_marker) == len(out) - 1 and "[context trimmed" not in json.dumps(no_marker)

    def test_always_keeps_newest_unit_even_if_huge(self):
        h = [{"role": "user", "content": "t"}, {"role": "assistant", "content": "x" * 100000}]
        assert SlidingWindow(64).apply(h)[-1] == h[-1]

    def test_no_user_head_and_leading_tool_orphan_free(self):
        h = [{"role": "assistant", "content": "a"}, {"role": "tool", "content": "t"}]
        assert SlidingWindow(1000).apply(h) == h

    def test_budget_validation_and_estimate(self):
        with pytest.raises(ValueError):
            SlidingWindow(10)
        assert estimate_tokens({"content": "x" * 400}) == 104
        assert estimate_tokens({"content": None, "tool_calls": [{"id": "x"}]}) > 4

    def test_broken_window_is_caught(self):
        assert not sc.window_ok(sc._BrokenWindow(1500), 1500)  # noqa: SLF001

    def test_windowed_client_applies_before_call(self):
        inner = ScriptedClient([sc._ok()], model="m")  # noqa: SLF001
        WindowedClient(inner, SlidingWindow(500)).chat(sc.long_history(30), temperature=0.1)
        assert len(inner.calls[0]["messages"]) < 61 and inner.calls[0]["temperature"] == 0.1


class TestJournalTraces:
    def ev(self, seq, kind, ts, **f):
        return {"seq": seq, "ts": ts, "kind": kind, **f}

    def test_ok_run(self):
        e = [
            self.ev(1, "run_started", 0),
            self.ev(2, "step_started", 1, step="a", attempt=1),
            self.ev(3, "step_completed", 3, step="a", attempt=1),
            self.ev(4, "run_completed", 4),
        ]
        s = spans_from_events("r1", e)
        assert (
            [x.kind for x in s] == ["run", "step"]
            and s[0].status == "ok"
            and s[1].duration == 2
            and s[1].parent_id == s[0].span_id
        )

    def test_failure_retry_unfinished_cancel(self):
        e = [
            self.ev(1, "run_started", 0),
            self.ev(2, "step_started", 1, step="a", attempt=1),
            self.ev(3, "step_failed", 2, step="a", error="x", retryable=True),
            self.ev(4, "step_started", 3, step="a", attempt=2, interrupted_before=True),
            self.ev(5, "step_started", 3.5, step="b", attempt=1),
            self.ev(6, "run_failed", 5, step="a", error="gave up"),
        ]
        s = spans_from_events("r2", e)
        assert s[0].status == "error" and s[0].error == "gave up" and s[0].attrs["retries"] == 1
        st = {(x.name, x.status) for x in s[1:]}
        assert ("a", "error") in st and ("a", "unfinished") in st and ("b", "unfinished") in st
        assert (
            spans_from_events("r3", [self.ev(1, "run_started", 0), self.ev(2, "run_cancelled", 1)])[0].status
            == "cancelled"
        )
        assert spans_from_events("r4", [self.ev(1, "run_started", 0)])[0].status == "running"
        assert spans_from_events("r5", []) == []

    def test_wait_timeout_closes_step(self):
        e = [
            self.ev(1, "run_started", 0),
            self.ev(2, "step_started", 1, step="w"),
            self.ev(3, "wait_timed_out", 9, step="w"),
        ]
        assert spans_from_events("r6", e)[1].status == "ok"

    def test_ids_deterministic(self):
        e = [
            self.ev(1, "run_started", 0),
            self.ev(2, "step_started", 1, step="a"),
            self.ev(3, "step_completed", 2, step="a"),
        ]
        assert [x.span_id for x in spans_from_events("r", e)] == [x.span_id for x in spans_from_events("r", e)]

    def test_real_engine_run_and_root_scan(self, tmp_path):
        from incident_agent.agenteval.rig import Rig, run_scenario
        from incident_agent.agenteval.scenarios import SCENARIOS

        scn = next(s for s in SCENARIOS if s.name == "approve")
        rig = Rig(tmp_path)
        run_scenario(rig, scn.name, "INC-001", scn.human)
        spans = spans_from_root(tmp_path / "runs")
        assert sum(s.kind == "run" for s in spans) == 1 and sum(s.kind == "step" for s in spans) >= 5
        (tmp_path / "runs" / "corrupt").mkdir()
        (tmp_path / "runs" / "corrupt" / "journal.jsonl").write_text('{"seq": 1}\n{"seq": 2, "x"\n{"seq":3}\n')
        assert len(spans_from_root(tmp_path / "runs")) == len(spans)  # one corrupt run does not hide the others
        assert spans_from_root(tmp_path / "missing") == []


class TestPrefix:
    def test_stable_volatile_changed_short(self):
        assert sc.check_prefix()
        short = pfx.analyze([{"messages": [{"role": "user", "content": "hi"}], "tools": None}])
        assert any("below the provider's minimum" in w for w in short["warnings"])
        assert pfx.analyze([])["calls"] == 0
        assert pfx.common_prefix_len("abcd", "abxd") == 2 and pfx.common_prefix_len("", "a") == 0


class TestReport:
    def spans(self):
        m = MemorySink()
        t = Tracer([m], clock=tick())
        p = PriceTable.load()
        with t.span("run:x", "run"):
            with t.span("diagnose", "step"):
                TracedClient(ScriptedClient([sc._ok("a", 1000, 100, 500)], model="openai/gpt-oss-20b"), t, p).chat(
                    [{"role": "user", "content": "q"}]
                )  # noqa: SLF001
                TracedClient(ScriptedClient([sc._ok()], model="llama-x"), t, p).chat([{"role": "user", "content": "q"}])  # noqa: SLF001
        t.event("cascade.route", "llm", tier="strong")
        return m.spans

    def test_summarize(self):
        s = rep.summarize(self.spans())
        assert s["llm"]["calls"] == 2 and s["llm"]["cached_tokens"] == 500 and s["cost"]["unpriced_calls"] == 1
        assert s["cascade"]["escalation_rate"] == 1.0 and s["runs"]["ok"] == 1 and "diagnose" in s["steps"]
        assert s["cost"]["total_usd"] == pytest.approx((500 * 0.075 + 500 * 0.0375 + 100 * 0.3) / 1e6)

    def test_empty_and_percentiles(self):
        s = rep.summarize([])
        assert s["llm"]["calls"] == 0 and s["llm"]["cache_hit_ratio"] == 0.0 and s["cascade"]["escalation_rate"] == 0.0
        assert (
            rep.pct([], 0.5) == 0.0
            and rep.pct([1, 3], 0.5) == 2
            and rep.pct([5], 0.95) == 5
            and rep.pct([1, 2, 3, 4], 1.0) == 4
        )

    def test_markdown_and_dashboard_are_safe_and_honest(self):
        s = rep.summarize(self.spans())
        md = rep.render_markdown(s)
        assert "unpriced" in md and "unknown, not zero" in md
        s["steps"]["<script>alert(1)</script>"] = {"count": 1, "p50": 1.0, "p95": 1.0}
        s["cost"]["by_model"]["<img src=x>"] = {
            "calls": 1,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "priced": False,
        }
        page = rep.render_dashboard(s, title="<b>t</b>")
        assert "<script" not in page.lower() and "<img" not in page and "&lt;script&gt;" in page and "http" not in page
        assert json.loads(rep.to_json(s))["spans"] == s["spans"]


class TestExporters:
    def test_langsmith_needs_env_and_pkg(self, monkeypatch):
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        with pytest.raises(ExporterUnavailable, match="LANGSMITH_API_KEY"):
            LangSmithSink()
        monkeypatch.setenv("LANGSMITH_API_KEY", "x")
        monkeypatch.setitem(__import__("sys").modules, "langsmith", None)
        with pytest.raises(ExporterUnavailable, match="pip install langsmith"):
            LangSmithSink()

    def test_otel_and_wandb_missing_pkg(self, monkeypatch):
        import sys

        for mod in ("opentelemetry", "wandb"):
            monkeypatch.setitem(sys.modules, mod, None)
        with pytest.raises(ExporterUnavailable):
            OtelSink()
        with pytest.raises(ExporterUnavailable):
            log_wandb({"a": 1})

    def test_wandb_defaults_offline_and_filters(self, monkeypatch, tmp_path):
        import sys

        monkeypatch.delenv("WANDB_MODE", raising=False)
        logged = {}

        class Run:
            dir = "d"

            def log(self, m):
                logged.update(m)

            def finish(self):
                logged["finished"] = True

        fake = types.SimpleNamespace(init=lambda **k: Run())
        monkeypatch.setitem(sys.modules, "wandb", fake)
        assert log_wandb({"a": 1, "b": "s", "c": True}, run_dir=tmp_path) == "d"
        import os

        assert os.environ["WANDB_MODE"] == "offline" and logged == {"a": 1, "c": True, "finished": True}
        monkeypatch.delenv("WANDB_MODE", raising=False)


class TestCli:
    def test_prices_demo_report(self, tmp_path, capsys):
        assert cli(["prices"]) == 0 and "openai/gpt-oss-120b" in capsys.readouterr().out
        assert cli(["demo", "--out", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "SIMULATED" in out and (tmp_path / "demo.html").exists() and (tmp_path / "demo.json").exists()
        assert cli(["report", "--trace", str(tmp_path / "demo-trace.jsonl"), "--out", str(tmp_path / "r")]) == 0
        assert (tmp_path / "r" / "observability.md").exists()

    def test_report_without_input_fails(self, capsys):
        assert cli(["report"]) == 2

    def test_report_from_runs(self, tmp_path, capsys):
        from incident_agent.agenteval.rig import Rig, run_scenario
        from incident_agent.agenteval.scenarios import SCENARIOS

        scn = next(s for s in SCENARIOS if s.name == "approve")
        run_scenario(Rig(tmp_path / "rig"), scn.name, "INC-001", scn.human)
        assert cli(["report", "--runs", str(tmp_path / "rig" / "runs"), "--out", str(tmp_path / "o")]) == 0

    def test_wandb_cli_unavailable(self, monkeypatch, capsys):
        import sys

        monkeypatch.setitem(sys.modules, "wandb", None)
        assert cli(["wandb"]) == 2


class TestGate:
    def test_section_passes_and_controls_flag(self):
        o = sc.run("INC-001", {})
        assert o["all_ok"] and o["all_flagged"] and len(o["controls"]) == 3 and o["journal_steps"] >= 5

    def test_redaction_check_detects_a_leak(self):
        assert sc.check_redaction() is True and sc.check_redaction_with_leaky_tracer() is False

    def test_trace_shape_and_never_escalates_controls(self):
        assert sc.check_trace_shape() and sc.check_never_escalates() is False

    def test_gate_logic(self):
        base = {
            "schema": suite.SCHEMA,
            "ids": ["a"],
            "observability": {"checks": sorted(sc.run("INC-001", {})["checks"]), "controls": 3, "cascade_saving": 0.25},
        }
        good = {"ids": ["a"], "observability": sc.run("INC-001", {})}
        assert suite.gate(good, base)[0] == []
        bad = json.loads(json.dumps(good))
        bad["observability"]["checks"]["window"] = False
        bad["observability"]["controls"][0]["flagged"] = False
        bad["observability"]["cascade_saving"] = 0.1
        del bad["observability"]["checks"]["prefix"]
        fails = " | ".join(suite.gate(bad, base)[0])
        for needle in ("'window' failed", "tracer_without_redaction", "cascade saving", "a check was removed"):
            assert needle in fails

    def test_baseline_records_section(self):
        rep_ = {"ids": ["a"], "observability": sc.run("INC-001", {})}
        assert suite.baseline_from(rep_)["observability"]["controls"] == 3
        assert "observability" in suite.SECTIONS

    def test_committed_baseline_has_section(self):
        b = suite.load_baseline()
        assert b["observability"]["cascade_saving"] == 0.25 and "redaction" in b["observability"]["checks"]


class TestExporterBodies:
    def span(self, status="ok"):
        return Span(
            "a" * 32,
            "b" * 16,
            "c" * 16,
            "n",
            "llm",
            1.0,
            2.0,
            status,
            "err" if status == "error" else None,
            {"k": "v", "n": 1, "o": {"x": 1}},
        )

    def test_langsmith_emit(self, monkeypatch):
        import sys

        calls = []

        class Client:
            def create_run(self, **kw):
                calls.append(kw)

        monkeypatch.setenv("LANGSMITH_API_KEY", "x")
        monkeypatch.setitem(sys.modules, "langsmith", types.SimpleNamespace(Client=Client))
        sink = LangSmithSink(project="p")
        sink.emit(self.span())
        root = self.span()
        root.parent_id = None
        root.kind = "run"
        sink.emit(root)
        assert (
            calls[0]["run_type"] == "llm" and calls[0]["project_name"] == "p" and calls[0]["parent_run_id"] is not None
        )
        assert (
            calls[1]["run_type"] == "chain"
            and calls[1]["parent_run_id"] is None
            and calls[0]["extra"]["metadata"]["k"] == "v"
        )

    def test_otel_emit(self, monkeypatch):
        import sys

        made = []

        class OSpan:
            def __init__(self, name, start_time):
                self.attrs, self.status, self.name, self.ended = {}, None, name, None

            def set_attribute(self, k, v):
                self.attrs[k] = v

            def set_status(self, s):
                self.status = s

            def end(self, end_time):
                self.ended = end_time

        class OTracer:
            def start_span(self, name, start_time):
                made.append(OSpan(name, start_time))
                return made[-1]

        class Provider:
            def __init__(self, resource=None):
                pass

            def add_span_processor(self, p):
                pass

            def get_tracer(self, n):
                return OTracer()

        mods = {
            "opentelemetry": types.SimpleNamespace(
                trace=types.SimpleNamespace(), Status=lambda c, d: (c, d), StatusCode=types.SimpleNamespace(ERROR="E")
            ),
            "opentelemetry.exporter.otlp.proto.http.trace_exporter": types.SimpleNamespace(
                OTLPSpanExporter=lambda endpoint: None
            ),
            "opentelemetry.sdk.resources": types.SimpleNamespace(Resource=types.SimpleNamespace(create=lambda d: d)),
            "opentelemetry.sdk.trace": types.SimpleNamespace(TracerProvider=Provider),
            "opentelemetry.sdk.trace.export": types.SimpleNamespace(SimpleSpanProcessor=lambda e: e),
        }
        for k, v in mods.items():
            monkeypatch.setitem(sys.modules, k, v)
        sink = OtelSink.__new__(OtelSink)
        sink._tracer = OTracer()  # noqa: SLF001
        sink._ot = mods["opentelemetry"]  # noqa: SLF001
        sink.emit(self.span("error"))
        s = made[0]
        assert s.attrs["k"] == "v" and s.attrs["n"] == 1 and "o" not in s.attrs and s.attrs["span.kind"] == "llm"
        assert s.status == ("E", "err") and s.ended == 2_000_000_000
