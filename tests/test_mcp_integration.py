"""The MCP servers wired into the ReAct agent and the harness (Stage 13): same requests, same results."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed (pip install -e .[mcp])")

from incident_agent.agents.llm_react_agent import LLMReActAgent  # noqa: E402
from incident_agent.eval import harness  # noqa: E402
from incident_agent.eval.harness import DiagnosisResult  # noqa: E402
from incident_agent.llm import LLMResponse, ScriptedClient, ToolCall  # noqa: E402
from incident_agent.mcp_servers.client import McpToolbox  # noqa: E402

PLAN = {
    "hypotheses": ["retry storm"],
    "steps": [{"tool": "log_overview", "purpose": "mix"}, {"tool": "search_logs", "purpose": "cpu"}],
}
FINAL = {
    "is_false_alarm": False,
    "root_cause": "An unbounded retry loop saturated the checkout service CPU.",
    "confidence": 0.8,
    "evidence": [{"source_type": "log_entry", "source": "logs.log", "detail": "retry", "timestamp": ""}],
    "affected_service": "checkout-api",
    "category": "cpu_exhaustion",
    "reasoning_steps": ["CPU rose first"],
    "recommendation": "Restart the pods and cap the retry loop.",
    "risk_tier": "high",
}


def script(replan: bool = False) -> ScriptedClient:
    return ScriptedClient(
        [
            LLMResponse(content=json.dumps(PLAN)),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c0", name="log_overview", arguments={}),
                    ToolCall(id="c1", name="search_logs", arguments={"query": "throttling", "limit": 2}),
                    ToolCall(id="c2", name="get_metric_summary", arguments={}),
                    ToolCall(id="c3", name="get_recent_deploys", arguments={}),
                    ToolCall(id="c4", name="get_incident_meta", arguments={}),
                    ToolCall(id="c5", name="search_logs", arguments={"query": ""}),  # a bad call the model must see
                ]
            ),
            *([LLMResponse(content=json.dumps(PLAN))] if replan else []),
            LLMResponse(content="ready"),
            LLMResponse(content=json.dumps(FINAL)),
        ]
    )


class RecordingFactory:
    def __init__(self) -> None:
        self.boxes: list[McpToolbox] = []
        self.closed = 0

    def __call__(self, incident_id, fail_tools=None):
        box = McpToolbox(incident_id, fail_tools=fail_tools)
        original = box.close

        def close():
            self.closed += 1
            original()

        box.close = close  # type: ignore[method-assign]
        self.boxes.append(box)
        return box


class TestAgentThroughMcp:
    def test_every_request_and_the_diagnosis_are_identical_to_the_direct_toolbox(self):
        direct_client, mcp_client = script(), script()
        direct = LLMReActAgent(direct_client)
        d1 = direct.run("INC-001")
        mcp_agent = LLMReActAgent(mcp_client, toolbox_factory=McpToolbox)
        d2 = mcp_agent.run("INC-001")
        assert d1.model_dump(exclude={"produced_at"}) == d2.model_dump(exclude={"produced_at"})
        assert direct.transcript == mcp_agent.transcript
        assert direct_client.calls == mcp_client.calls  # identical requests => identical LLM cache keys

    def test_the_toolbox_is_closed_after_the_run(self):
        factory = RecordingFactory()
        LLMReActAgent(script(), toolbox_factory=factory).run("INC-001")
        assert len(factory.boxes) == 1 and factory.closed == 1 and factory.boxes[0]._stack is None

    def test_the_toolbox_is_closed_when_the_provider_goes_away(self):
        from incident_agent.llm import LLMUnavailable

        class Dying(ScriptedClient):
            def chat(self, *a, **k):
                if len(self.calls) >= 2:
                    raise LLMUnavailable("quota")
                return super().chat(*a, **k)

        factory = RecordingFactory()
        client = Dying(
            [
                LLMResponse(content=json.dumps(PLAN)),
                LLMResponse(tool_calls=[ToolCall(id="c0", name="log_overview", arguments={})]),
            ]
        )
        agent = LLMReActAgent(client, toolbox_factory=factory)
        try:
            agent.run("INC-001")
        except Exception:  # the final structured call has nothing scripted; only the closing matters here
            pass
        assert factory.closed == 1

    def test_injected_tool_failures_behave_the_same_through_mcp(self):
        a = LLMReActAgent(script(replan=True), fail_tools={"get_metric_summary"})
        b = LLMReActAgent(script(replan=True), fail_tools={"get_metric_summary"}, toolbox_factory=McpToolbox)
        a.run("INC-001")
        b.run("INC-001")
        assert a.transcript == b.transcript and a.unavailable == b.unavailable == {"get_metric_summary"}


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(harness, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(harness, "OPENINGS_LOG", tmp_path / "openings.log")
    return tmp_path


class TestHarness:
    def test_default_call_signature_is_unchanged(self, sandbox, monkeypatch):
        seen = []

        def fake(name, temperature=0.0, risk_guard=False):
            seen.append((name, temperature, risk_guard))
            return lambda iid: DiagnosisResult(iid, "Unknown")

        monkeypatch.setattr(harness, "make_adapter", fake)
        assert harness.main(["--agent", "llm_react", "--ids", "INC-001", "--tag", "d"]) == 0
        assert seen == [("llm_react", 0.0, False)]
        meta = json.loads((sandbox / "results" / "llm_react__d.json").read_text())["meta"]
        assert meta["toolbox"] == "direct"

    def test_mcp_flag_is_passed_through_and_recorded(self, sandbox, monkeypatch):
        seen = []

        def fake(name, temperature=0.0, risk_guard=False, toolbox="direct"):
            seen.append((name, toolbox))
            return lambda iid: DiagnosisResult(iid, "Unknown")

        monkeypatch.setattr(harness, "make_adapter", fake)
        assert harness.main(["--agent", "llm_react", "--toolbox", "mcp", "--ids", "INC-001", "--tag", "m"]) == 0
        assert seen == [("llm_react", "mcp")]
        assert json.loads((sandbox / "results" / "llm_react__m.json").read_text())["meta"]["toolbox"] == "mcp"

    def test_mcp_is_rejected_for_agents_that_do_not_support_it_yet(self, sandbox, capsys):
        assert harness.main(["--agent", "react_baseline", "--toolbox", "mcp", "--ids", "INC-001"]) == 2
        assert "only wired into llm_react" in capsys.readouterr().out
        with pytest.raises(ValueError, match="llm_react"):
            harness.make_adapter("llm_orchestrator", toolbox="mcp")

    def test_llm_react_adapter_builds_an_mcp_agent(self, monkeypatch):
        import incident_agent.llm as llm

        monkeypatch.setattr(llm, "build_client", script)
        run = harness.make_adapter("llm_react", toolbox="mcp")
        result = run("INC-001")
        assert not result.error and result.root_cause.startswith("An unbounded retry loop")
