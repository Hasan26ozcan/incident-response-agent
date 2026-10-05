"""Stage 3-5 behaviour of the LLM ReAct agent, driven by a scripted (offline) LLM."""

import json

import pytest
from pydantic import BaseModel

from incident_agent.agents.llm_react_agent import LLMReActAgent
from incident_agent.llm import (
    CachedClient,
    LLMResponse,
    OpenAICompatClient,
    ScriptedClient,
    StructuredOutputError,
    ToolCall,
    generate_structured,
)
from incident_agent.schemas.agent_output import RiskTier
from incident_agent.tools.toolbox import IncidentToolbox, ToolError

PLAN = {
    "hypotheses": ["retry storm", "bad deploy"],
    "steps": [
        {"tool": "log_overview", "purpose": "see error mix"},
        {"tool": "get_metric_summary", "purpose": "timing"},
    ],
}
REPLAN = {
    "hypotheses": ["retry storm"],
    "steps": [{"tool": "log_overview", "purpose": "metrics down; rely on logs"}],
}


def _json(obj) -> LLMResponse:
    return LLMResponse(content=json.dumps(obj))


def _calls(*names) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)])


READY = LLMResponse(content="ready to conclude")


def _final(**over) -> LLMResponse:
    body = {
        "is_false_alarm": False,
        "root_cause": "An unbounded retry loop saturated the checkout service CPU.",
        "confidence": 0.8,
        "evidence": [{"source_type": "log_entry", "source": "logs.log", "detail": "retry", "timestamp": ""}],
        "affected_service": "checkout-api",
        "category": "cpu_exhaustion",
        "reasoning_steps": ["CPU rose first", "no deploy changes"],
        "recommendation": "Restart the pods and cap the retry loop.",
        "risk_tier": "high",
    }
    body.update(over)
    return _json(body)


def test_happy_path_returns_validated_diagnosis():
    client = ScriptedClient([_json(PLAN), _calls("log_overview", "get_metric_summary"), READY, _final()])
    d = LLMReActAgent(client).run("INC-001")
    assert d.agent_type == "llm_react" and d.incident_id == "INC-001"
    assert d.risk_tier is RiskTier.HIGH
    assert any("log_overview" in t for t in client.calls[-1]["messages"][1]["content"].split("\n\n"))


def test_structured_final_call_never_carries_tools():
    client = ScriptedClient([_json(PLAN), READY, _final()])
    LLMReActAgent(client).run("INC-001")
    for call in client.calls:
        assert not (call["tools"] and call["schema"]), "Groq cannot combine tools and structured output"


def test_replans_when_a_tool_fails_and_withdraws_it():
    client = ScriptedClient([_json(PLAN), _calls("get_metric_summary"), _json(REPLAN), READY, _final()])
    agent = LLMReActAgent(client, fail_tools={"get_metric_summary"})
    d = agent.run("INC-001")
    assert len(agent.plan_history) == 2
    assert "get_metric_summary" in agent.unavailable
    tool_names_after = [t["function"]["name"] for t in client.calls[3]["tools"]]
    assert "get_metric_summary" not in tool_names_after
    assert d.root_cause


def test_replan_budget_is_respected():
    client = ScriptedClient(
        [_json(PLAN), _calls("get_metric_summary"), _json(REPLAN), _calls("get_recent_deploys"), READY, _final()]
    )
    agent = LLMReActAgent(client, max_replans=1, fail_tools={"get_metric_summary", "get_recent_deploys"})
    agent.run("INC-001")
    assert len(agent.plan_history) == 2  # initial + one replan, second failure only withdraws the tool
    assert agent.unavailable == {"get_metric_summary", "get_recent_deploys"}


def test_unknown_tool_is_reported_not_crashed():
    client = ScriptedClient([_json(PLAN), _calls("rm_rf"), READY, _final()])
    d = LLMReActAgent(client).run("INC-001")
    assert d.incident_id == "INC-001"


def test_invalid_final_json_is_repaired():
    client = ScriptedClient([_json(PLAN), READY, LLMResponse(content='{"root_cause": "x"}'), _final()])
    d = LLMReActAgent(client).run("INC-001")
    assert len(client.calls) == 4 and d.root_cause


def test_repair_budget_exhaustion_raises():
    bad = LLMResponse(content="not json")
    client = ScriptedClient([bad, bad, bad])

    class Strict(BaseModel):
        value: int

    with pytest.raises(StructuredOutputError):
        generate_structured(client, [{"role": "user", "content": "x"}], Strict)


def test_hallucinated_evidence_lowers_confidence():
    fake = {"source_type": "log_entry", "source": "logs.log", "detail": "kernel panic on node-99 zzqx", "timestamp": ""}
    client = ScriptedClient([_json(PLAN), _calls("log_overview"), READY, _final(evidence=[fake])])
    d = LLMReActAgent(client).run("INC-001")
    assert d.confidence < 0.8 and d.evidence[0].confidence_weight < 1.0
    assert any("Grounding check" in s.description for s in d.reasoning_steps)


def test_false_alarm_root_cause_is_marked():
    client = ScriptedClient([_json(PLAN), READY, _final(is_false_alarm=True, risk_tier="low")])
    d = LLMReActAgent(client).run("INC-019")
    assert d.root_cause.lower().startswith("false alarm") and d.risk_tier is RiskTier.LOW


def test_toolbox_rejects_bad_ids_and_cannot_reach_gold():
    with pytest.raises(ValueError):
        IncidentToolbox("../eval/gold")
    with pytest.raises(ValueError):
        IncidentToolbox("INC-999")


def test_toolbox_tools_work_and_clip():
    tb = IncidentToolbox("INC-001")
    assert "ERROR" in tb.call("log_overview", {})
    assert "matches" in tb.call("search_logs", {"query": "e", "level": "ERROR", "limit": 3})
    assert len(tb.call("get_metric_summary", {})) < 4000
    with pytest.raises(ToolError):
        tb.call("search_logs", {"nope": 1})


def test_tools_and_schema_cannot_be_combined():
    client = OpenAICompatClient("m", "k", "http://localhost:1")
    with pytest.raises(ValueError):
        client.chat([], tools=[{"x": 1}], response_schema=object)  # type: ignore[arg-type]


def test_cache_hits_skip_the_provider(tmp_path):
    inner = ScriptedClient([LLMResponse(content="hi")])
    cached = CachedClient(inner, tmp_path)
    msgs = [{"role": "user", "content": "q"}]
    assert cached.chat(msgs).content == "hi"
    assert cached.chat(msgs).content == "hi"  # would raise AssertionError if it reached the exhausted fake
    assert (cached.hits, cached.misses) == (1, 1)


def test_dotenv_loader_sets_missing_but_never_overrides(tmp_path, monkeypatch):
    from incident_agent.llm.client import _load_dotenv

    f = tmp_path / ".env"
    f.write_text('# c\nNEW_VAR_X="abc"\nKEEP_VAR_X=from_file\n')
    monkeypatch.delenv("NEW_VAR_X", raising=False)
    monkeypatch.setenv("KEEP_VAR_X", "from_env")
    _load_dotenv(f)
    import os

    assert os.environ["NEW_VAR_X"] == "abc" and os.environ["KEEP_VAR_X"] == "from_env"
    monkeypatch.delenv("NEW_VAR_X")


def test_dotenv_handles_windows_bom_crlf_export_and_empty_values(tmp_path, monkeypatch):
    from incident_agent.llm.client import _load_dotenv

    f = tmp_path / ".env"
    f.write_bytes(b"\xef\xbb\xbfBOM_VAR_X=secret123\r\nEMPTY_VAR_X=\r\nexport EXP_VAR_X='q'\r\n")
    for key in ("BOM_VAR_X", "EMPTY_VAR_X", "EXP_VAR_X"):
        monkeypatch.delenv(key, raising=False)

    _load_dotenv(f)

    import os

    assert os.environ["BOM_VAR_X"] == "secret123"
    assert os.environ["EXP_VAR_X"] == "q"
    assert "EMPTY_VAR_X" not in os.environ
