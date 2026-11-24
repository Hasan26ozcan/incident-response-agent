"""Stage 21d: the measurement harness, tested with fake providers (no keys, no network)."""

from __future__ import annotations

import json

import pytest

from incident_agent.eval import harness
from incident_agent.llm import LLMResponse, ScriptedClient
from incident_agent.llm.client import LLMConfigError, LLMUnavailable
from incident_agent.measure import __main__ as mcli
from incident_agent.measure import campaign as camp
from incident_agent.measure import report as rep
from incident_agent.measure import runner
from incident_agent.measure import spec as sp
from incident_agent.measure.campaign import Config
from incident_agent.observe.cascade import CascadeClient
from incident_agent.observe.llm import TracedClient
from incident_agent.observe.pricing import PriceTable

USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 200}
G20, G120 = "groq/openai/gpt-oss-20b", "groq/openai/gpt-oss-120b"


def scripted(model: str, n: int = 400, text: str = "good") -> ScriptedClient:
    return ScriptedClient([LLMResponse(content=text, usage=dict(USAGE))] * n, model=model)


def factory(spec: str, tracer):
    prices = PriceTable.load()
    if sp.is_cascade(spec):
        cheap, strong = sp.parse_cascade(spec)
        return CascadeClient(
            TracedClient(scripted(cheap.model, text="bad"), tracer, prices, "cheap"),
            TracedClient(scripted(strong.model), tracer, prices, "strong"),
            accept=lambda r, s: r.content == "good",
            tracer=tracer,
        )
    return TracedClient(scripted(sp.parse(spec).model), tracer, prices, "llm")


def oracle_adapter(agent, client, *, truthful: bool = True):
    """Each incident: one model call, then answer with the gold root cause (or a wrong one)."""
    gold = {}
    for d in (harness.GOLD_DIR, camp.LAB_DATA / "gold"):
        for p in d.glob("*.json"):
            gold[p.stem] = json.loads(p.read_text())

    def diagnose(incident_id):
        client.chat([{"role": "user", "content": incident_id}])
        g = gold[incident_id]
        text = g["root_cause"] if truthful else "something unrelated entirely"
        return harness.DiagnosisResult(incident_id, text, g["evidence"], g["remediation_risk_tier"])

    return diagnose


class TestSpec:
    def test_parse(self):
        assert sp.parse(G120) == sp.ModelSpec("groq", "openai/gpt-oss-120b") and str(sp.parse(G120)) == G120
        for bad in ("gpt", "nope/x", "groq/", ""):
            with pytest.raises(ValueError):
                sp.parse(bad)

    def test_cascade(self):
        spec = f"cascade:{G20}>{G120}"
        assert sp.is_cascade(spec) and not sp.is_cascade(G20)
        assert sp.parse_cascade(spec)[1].model == "openai/gpt-oss-120b"
        with pytest.raises(ValueError):
            sp.parse_cascade("cascade:groq/a")

    def test_required_keys_names_only(self):
        assert sp.required_keys(G120) == ["GROQ_API_KEY"]
        assert sp.required_keys(f"cascade:{G20}>mistral/mistral-large-latest") == ["GROQ_API_KEY", "MISTRAL_API_KEY"]

    def test_build_needs_env_key_and_builds_clients(self, monkeypatch):
        from incident_agent.observe.trace import Tracer

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
            sp.build_one(sp.parse(G120))
        monkeypatch.setenv("GROQ_API_KEY", "k")
        monkeypatch.setenv("MISTRAL_API_KEY", "k")
        monkeypatch.setenv("MISTRAL_MIN_INTERVAL", "0")
        t = Tracer()
        assert sp.build_client(G120, t).model == "openai/gpt-oss-120b"
        assert sp.build_client("mistral/mistral-large-latest", t).model == "mistral-large-latest"
        casc = sp.build_client(f"cascade:{G20}>{G120}", t)
        assert isinstance(casc, CascadeClient) and casc.model == "openai/gpt-oss-20b"


class TestCampaign:
    def test_sets_never_include_test_split(self):
        dev, lab = camp.set_ids("dev"), camp.set_ids("lab")
        test = set(harness.load_split("test"))
        assert len(dev) == 14 and not (set(dev) & test) and lab == [f"LAB-00{i}" for i in range(1, 6)]
        with pytest.raises(ValueError, match="test split"):
            camp.set_ids("test")

    def test_plan(self):
        p = camp.plan("cascade", ["dev", "lab"])
        assert p["sets"] == {"dev": 14, "lab": 5} and len(p["configs"]) == 3 and p["est_total_calls"] > 0
        assert "estimate only" in camp.render_plan(p) and json.loads(camp.dumps(p))["campaign"] == "cascade"

    def test_every_campaign_uses_valid_specs_and_agents(self):
        for name, configs in camp.CAMPAIGNS.items():
            assert configs and len({c.name for c in configs}) == len(configs), name
            for c in configs:
                assert c.agent in harness.ADAPTER_NAMES
                sp.required_keys(c.model)


class TestRunner:
    def run(self, tmp_path, config, set_name="lab", **kw):
        return runner.run_config(
            config, set_name, tmp_path, client_factory=factory, adapter_factory=oracle_adapter, **kw
        )

    def test_lab_run_writes_scores_cost_and_dashboard(self, tmp_path):
        path = self.run(tmp_path, Config("react-120b", "llm_react", G120))
        body = json.loads(path.read_text())
        assert (
            body["meta"]["complete"]
            and body["meta"]["cache"] == "off"
            and body["summary"]["n"] == 5
            and body["summary"]["crashed"] == 0
        )
        assert body["summary"]["root_cause_f1"] > 0.95
        e = body["measure"]["LAB-001"]
        assert e["calls"] == 1 and e["unpriced_calls"] == 0
        assert e["cost_usd"] == pytest.approx((800 * 0.15 + 200 * 0.075 + 100 * 0.6) / 1e6)
        assert (tmp_path / "react-120b__lab.observability.html").exists() and (
            tmp_path / "react-120b__lab.trace.jsonl"
        ).exists()

    def test_dev_set_uses_dataset_gold(self, tmp_path):
        body = json.loads(self.run(tmp_path, Config("c", "llm_react", G20), "dev").read_text())
        assert body["summary"]["n"] == 14 and {r["incident_id"] for r in body["incidents"]} == set(camp.set_ids("dev"))

    def test_resume_skips_finished_and_reruns_crashed(self, tmp_path):
        cfg = Config("c", "llm_react", G120)
        calls = []
        failed = []

        def counting(agent, client):
            inner = oracle_adapter(agent, client)

            def diagnose(iid):
                calls.append(iid)
                if iid == "LAB-003" and not failed:
                    failed.append(iid)
                    raise RuntimeError("rate limited")
                return inner(iid)

            return diagnose

        path = runner.run_config(cfg, "lab", tmp_path, client_factory=factory, adapter_factory=counting)
        assert json.loads(path.read_text())["summary"]["crashed"] == 1
        calls.clear()
        runner.run_config(cfg, "lab", tmp_path, resume=True, client_factory=factory, adapter_factory=counting)
        assert calls == ["LAB-003"]  # only the crashed one ran again
        assert (
            json.loads(path.read_text())["summary"]["crashed"] == 0
            and len(json.loads(path.read_text())["incidents"]) == 5
        )

    def test_partial_file_is_written_after_each_incident(self, tmp_path):
        cfg = Config("c", "llm_react", G120)
        seen = []

        def spying(agent, client):
            inner = oracle_adapter(agent, client)

            def diagnose(iid):
                p = tmp_path / "c__lab.json"
                seen.append(len(json.loads(p.read_text())["incidents"]) if p.exists() else 0)
                return inner(iid)

            return diagnose

        runner.run_config(cfg, "lab", tmp_path, client_factory=factory, adapter_factory=spying)
        assert seen == [0, 1, 2, 3, 4]

    def test_pace_and_ids_subset(self, tmp_path, monkeypatch):
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
        runner.run_config(
            Config("c", "llm_react", G120),
            "lab",
            tmp_path,
            ids=["LAB-001", "LAB-002"],
            pace=1.5,
            client_factory=factory,
            adapter_factory=oracle_adapter,
        )
        assert slept == [1.5, 1.5]

    def test_unpriced_model_is_counted_not_free(self, tmp_path):
        def f(spec, tracer):
            return TracedClient(scripted("llama-unpriced"), tracer, PriceTable.load(), "llm")

        path = runner.run_config(
            Config("u", "llm_react", G120),
            "lab",
            tmp_path,
            ids=["LAB-001"],
            client_factory=f,
            adapter_factory=oracle_adapter,
        )
        e = json.loads(path.read_text())["measure"]["LAB-001"]
        assert e["unpriced_calls"] == 1 and e["cost_usd"] == 0.0


class TestReport:
    def campaign(self, tmp_path):
        runner.run_config(
            Config("react-120b", "llm_react", G120),
            "lab",
            tmp_path,
            client_factory=factory,
            adapter_factory=oracle_adapter,
        )
        runner.run_config(
            Config("react-20b", "llm_react", G20),
            "lab",
            tmp_path,
            client_factory=factory,
            adapter_factory=lambda a, c: oracle_adapter(a, c, truthful=False),
        )
        runner.run_config(
            Config("react-cascade", "llm_react", f"cascade:{G20}>{G120}"),
            "lab",
            tmp_path,
            client_factory=factory,
            adapter_factory=oracle_adapter,
        )
        return tmp_path

    def test_bootstrap_is_deterministic_and_sane(self):
        assert rep.bootstrap_ci([]) == (0.0, 0.0) and rep.bootstrap_ci([0.5] * 10) == (0.5, 0.5)
        lo, hi = rep.bootstrap_ci([0.0, 1.0] * 10)
        assert lo < 0.5 < hi and rep.bootstrap_ci([0.0, 1.0] * 10) == (lo, hi)

    def test_comparison_and_cascade_saving(self, tmp_path):
        out = rep.build(self.campaign(tmp_path), "react-120b")
        lab = out["sets"]["lab"]
        assert lab["reference"] == "react-120b" and "vs_reference" not in lab["configs"]["react-120b"]
        worse = lab["configs"]["react-20b"]["vs_reference"]
        assert worse["mean_diff"] < -0.5 and worse["losses"] == 5 and worse["wins"] == 0
        same = lab["configs"]["react-cascade"]
        assert same["vs_reference"]["ties"] == 5
        c = same["cascade"]
        assert c["strong_call_share"] == 0.5 and c["saving"] is not None and c["always_strong_cost_usd"] > 0
        # cheap is tried and rejected, then strong runs: costs more than strong alone (an honest negative saving)
        assert c["saving"] < 0

    def test_markdown_states_limits(self, tmp_path):
        text = rep.render_markdown(rep.build(self.campaign(tmp_path), "react-120b"))
        assert "react-cascade" in text and "bootstrap" in text and "within noise" in text and "never as free" in text
        assert "reached the strong model" in text and "saving" in text

    def test_reference_default_and_crashes_excluded(self, tmp_path):
        runner.run_config(
            Config("only", "llm_react", G120), "lab", tmp_path, client_factory=factory, adapter_factory=oracle_adapter
        )
        assert rep.build(tmp_path)["sets"]["lab"]["reference"] == "only"
        run = rep.load(tmp_path / "only__lab.json")
        run["incidents"][0]["error"] = "boom"
        s = rep.summarize_run(run)
        assert s["crashed"] == 1 and s["n"] == 5

    def test_cascade_view_needs_two_models(self, tmp_path):
        runner.run_config(
            Config("react-120b", "llm_react", G120),
            "lab",
            tmp_path,
            client_factory=factory,
            adapter_factory=oracle_adapter,
        )
        run = rep.load(tmp_path / "react-120b__lab.json")
        assert rep.cascade_view(run, run) is None


class TestCli:
    def test_plan_and_unknown_sets(self, capsys):
        assert mcli.main(["plan", "smoke"]) == 0 and "react-120b" in capsys.readouterr().out
        assert mcli.main(["plan", "smoke", "--sets", "test"]) == 2
        assert "milestones" in capsys.readouterr().err

    def test_run_needs_keys_by_name_only(self, monkeypatch, capsys, tmp_path):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr(mcli, "_load_dotenv", lambda: None)
        assert mcli.main(["run", "smoke", "--out", str(tmp_path)]) == 2
        err = capsys.readouterr().err
        assert "GROQ_API_KEY" in err and "never paste" in err

    def test_run_and_report_end_to_end_with_fakes(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GROQ_API_KEY", "x")
        monkeypatch.setattr(mcli, "_load_dotenv", lambda: None)
        real = runner.run_config
        monkeypatch.setattr(
            runner,
            "run_config",
            lambda *a, **k: real(*a, **{**k, "client_factory": factory, "adapter_factory": oracle_adapter}),
        )
        assert mcli.main(["run", "smoke", "--sets", "lab", "--out", str(tmp_path)]) == 0
        md = tmp_path / "m.md"
        assert mcli.main(["report", "smoke", "--out", str(tmp_path), "--markdown", str(md)]) == 0
        assert "react-120b" in md.read_text()

    def test_run_stops_cleanly_on_provider_error_and_unknown_only(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GROQ_API_KEY", "x")
        monkeypatch.setattr(mcli, "_load_dotenv", lambda: None)
        monkeypatch.setattr(runner, "run_config", lambda *a, **k: (_ for _ in ()).throw(LLMUnavailable("429")))
        assert mcli.main(["run", "smoke", "--sets", "lab", "--out", str(tmp_path)]) == 1
        assert "--resume" in capsys.readouterr().err
        assert mcli.main(["run", "smoke", "--only", "nope", "--out", str(tmp_path)]) == 2

    def test_report_without_results(self, tmp_path, capsys):
        assert mcli.main(["report", "smoke", "--out", str(tmp_path)]) == 2


def fake_run(scores_by_id: dict[str, tuple[float, str]], model: str = "m") -> dict:
    rows = [
        {
            "incident_id": i,
            "root_cause_f1": f,
            "evidence_recall": f,
            "risk_match": True,
            "false_alarm_ok": True,
            "answered": True,
            "error": err,
        }
        for i, (f, err) in scores_by_id.items()
    ]
    return {
        "meta": {"model": f"groq/{model}", "set": "lab", "config": "c", "complete": True},
        "incidents": rows,
        "measure": {},
    }


class TestReportEdges:
    def test_threshold_is_exactly_point_zero_two(self):
        ref = fake_run({"a": (0.50, ""), "b": (0.50, ""), "c": (0.50, "")})
        other = fake_run({"a": (0.53, ""), "b": (0.47, ""), "c": (0.51, "")})
        p = rep.paired(ref, other)
        assert (p["wins"], p["losses"], p["ties"]) == (1, 1, 1)  # +0.03 win, -0.03 loss, +0.01 tie

    def test_crashed_incidents_are_excluded_from_pairs_on_either_side(self):
        ref = fake_run({"a": (0.5, ""), "b": (0.5, "boom")})
        other = fake_run({"a": (0.9, "boom"), "b": (0.9, "")})
        assert rep.paired(ref, other)["n"] == 0 and rep.paired(ref, ref)["n"] == 1

    def test_crashed_incidents_do_not_count_in_quality_means(self):
        run = fake_run({"a": (1.0, ""), "b": (0.0, "boom")})
        assert rep.summarize_run(run)["root_cause_f1"] == 1.0 and rep.summarize_run(run)["crashed"] == 1

    def test_a_fresh_run_discards_the_previous_trace_but_resume_keeps_it(self, tmp_path):
        cfg = Config("c", "llm_react", G120)
        trace = tmp_path / "c__lab.trace.jsonl"
        trace.parent.mkdir(exist_ok=True)
        trace.write_text("STALE\n")
        runner.run_config(cfg, "lab", tmp_path, ids=["LAB-001"], client_factory=factory, adapter_factory=oracle_adapter)
        assert "STALE" not in trace.read_text()
        n = len(trace.read_text().splitlines())
        runner.run_config(
            cfg, "lab", tmp_path, ids=["LAB-001"], resume=True, client_factory=factory, adapter_factory=oracle_adapter
        )
        assert len(trace.read_text().splitlines()) >= n
