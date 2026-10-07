"""Harness tooling: frozen split, tagged results, incremental save/resume, traces, usage, temperature."""

import json
from collections import Counter

import pytest

from incident_agent.agents.llm_react_agent import LLMReActAgent
from incident_agent.eval import harness
from incident_agent.eval.harness import DiagnosisResult, evaluate, load_split, result_path
from incident_agent.llm import LLMResponse, ScriptedClient, TrackingClient

USAGE = {"calls": 2, "prompt_tokens": 10, "completion_tokens": 5}


def _fake(iid: str) -> DiagnosisResult:
    return DiagnosisResult(iid, "Unknown", trace={"incident": iid}, usage=dict(USAGE))


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(harness, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(harness, "OPENINGS_LOG", tmp_path / "openings.log")
    monkeypatch.setattr(harness, "make_adapter", lambda name, temperature=0.0: _fake)
    return tmp_path


def _gold_ids() -> list[str]:
    return sorted(p.stem for p in harness.GOLD_DIR.glob("INC-*.json"))


def test_split_is_disjoint_complete_and_balanced():
    dev, test = load_split("dev"), load_split("test")
    assert not set(dev) & set(test)
    assert sorted(dev + test) == _gold_ids()
    assert len(dev) == len(test) == 10
    for split in (dev, test):
        diffs = Counter(json.loads((harness.GOLD_DIR / f"{i}.json").read_text())["difficulty"] for i in split)
        assert diffs["hard"] >= 2 and diffs["easy"] >= 2  # both halves contain easy and hard cases


def test_unknown_split_rejected():
    with pytest.raises(ValueError):
        load_split("holdout")


def test_ids_filter_and_unknown_ids():
    rep = evaluate(_fake, ids=["INC-004", "INC-001"])
    assert [r["incident_id"] for r in rep["incidents"]] == ["INC-001", "INC-004"]
    with pytest.raises(ValueError, match="INC-999"):
        evaluate(_fake, ids=["INC-999"])


def test_tag_validation_and_paths():
    assert result_path("llm_react", "v1_dev").name == "llm_react__v1_dev.json"
    assert result_path("llm_react", None).name == "llm_react.json"
    with pytest.raises(ValueError):
        result_path("llm_react", "../evil")


def test_main_writes_tagged_report_with_meta_and_usage(sandbox):
    harness.main(["--agent", "llm_react", "--split", "dev", "--tag", "t1"])
    rep = json.loads((sandbox / "results" / "llm_react__t1.json").read_text())
    assert rep["meta"]["complete"] is True and rep["meta"]["split"] == "dev" and rep["meta"]["tag"] == "t1"
    assert rep["summary"]["n"] == 10 and rep["summary"]["llm_calls"] == 20
    assert rep["summary"]["prompt_tokens"] == 100
    assert not (sandbox / "openings.log").exists()  # dev does not count as a test opening


def test_opening_test_split_is_logged(sandbox, capsys):
    harness.main(["--agent", "llm_react", "--split", "test", "--tag", "t2", "--limit", "2"])
    log = (sandbox / "openings.log").read_text()
    assert "tag=t2" in log and "n=10" in log
    assert "TEST SET OPENED" in capsys.readouterr().out


def test_incremental_save_and_resume(sandbox):
    calls: list[str] = []

    def flaky(iid: str) -> DiagnosisResult:
        calls.append(iid)
        if len(calls) == 3:
            raise KeyboardInterrupt  # simulates quota cut-off / Ctrl+C (not caught as a crash)
        return _fake(iid)

    harness.make_adapter = lambda name, temperature=0.0: flaky  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt):
        harness.main(["--agent", "llm_react", "--split", "dev", "--tag", "r"])
    path = sandbox / "results" / "llm_react__r.json"
    partial = json.loads(path.read_text())
    assert partial["meta"]["complete"] is False and len(partial["incidents"]) == 2

    resumed: list[str] = []

    def ok(iid: str) -> DiagnosisResult:
        resumed.append(iid)
        return _fake(iid)

    harness.make_adapter = lambda name, temperature=0.0: ok  # type: ignore[assignment]
    harness.main(["--agent", "llm_react", "--split", "dev", "--tag", "r", "--resume"])
    final = json.loads(path.read_text())
    assert final["meta"]["complete"] is True and len(final["incidents"]) == 10
    assert len(resumed) == 8  # the 2 finished incidents were not re-run


def test_trace_files_only_with_flag(sandbox):
    harness.main(["--agent", "llm_react", "--ids", "INC-001", "--tag", "a"])
    assert not (sandbox / "traces").exists()
    harness.main(["--agent", "llm_react", "--ids", "INC-001", "--tag", "b", "--trace"])
    assert json.loads((sandbox / "traces" / "b" / "INC-001.json").read_text()) == {"incident": "INC-001"}


def test_adapter_reported_error_counts_as_crash():
    def boom(iid: str) -> DiagnosisResult:
        return DiagnosisResult(iid, "Unknown", error="RuntimeError: provider exploded", trace={"t": 1})

    rep = evaluate(boom, limit=2)
    assert rep["summary"]["crashed"] == 2 and "provider exploded" in rep["incidents"][0]["error"]


def test_tracking_client_sums_usage():
    u = {"prompt_tokens": 7, "completion_tokens": 3}
    inner = ScriptedClient([LLMResponse(content="a", usage=u), LLMResponse(content="b", usage=u)])
    tc = TrackingClient(inner)
    tc.chat([{"role": "user", "content": "x"}])
    tc.chat([{"role": "user", "content": "y"}])
    assert tc.summary() == {"calls": 2, "prompt_tokens": 14, "completion_tokens": 6}


def test_temperature_reaches_every_llm_call():
    plan = {"hypotheses": ["h"], "steps": [{"tool": "log_overview", "purpose": "p"}]}
    final = {
        "is_false_alarm": False,
        "root_cause": "An unbounded retry loop saturated the checkout service CPU.",
        "confidence": 0.7,
        "evidence": [{"source_type": "log_entry", "source": "logs.log", "detail": "retry", "timestamp": ""}],
        "affected_service": "checkout-api",
        "category": "cpu_exhaustion",
        "reasoning_steps": ["a"],
        "recommendation": "Restart the pods and cap the retry loop.",
        "risk_tier": "high",
    }
    client = ScriptedClient(
        [LLMResponse(content=json.dumps(plan)), LLMResponse(content="ready"), LLMResponse(content=json.dumps(final))]
    )
    LLMReActAgent(client, temperature=0.4).run("INC-001")
    assert [c["temperature"] for c in client.calls] == [0.4, 0.4, 0.4]
