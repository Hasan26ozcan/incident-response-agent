from __future__ import annotations

from incident_agent import cli
from incident_agent.llm import LLMUnavailable


def test_diagnose_loads_dotenv_before_selecting_default_agent(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda: monkeypatch.setenv("GROQ_API_KEY", "test-key"))

    class Diagnosis:
        def format_report(self) -> str:
            return "diagnosis"

    class FakeAgent:
        def __init__(self, client, risk_guard=False) -> None:
            assert client == "test-client" and risk_guard is False

        def run(self, incident_id: str) -> Diagnosis:
            assert incident_id == "INC-001"
            return Diagnosis()

    monkeypatch.setattr("incident_agent.llm.build_client", lambda: "test-client")
    monkeypatch.setattr("incident_agent.agents.llm_react_agent.LLMReActAgent", FakeAgent)

    assert cli._cmd_diagnose(["INC-001"]) == 0
    assert capsys.readouterr().out == "diagnosis\n"


def test_diagnose_reports_missing_llm_credentials_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "incident_agent.llm.build_client",
        lambda: (_ for _ in ()).throw(LLMUnavailable("set GROQ_API_KEY")),
    )

    assert cli._cmd_diagnose(["INC-001"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "LLM unavailable: set GROQ_API_KEY\n"
    assert captured.out == ""


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        ("llm", "incident_agent.agents.llm_react_agent.LLMReActAgent"),
        ("orchestrator", "incident_agent.agents.llm_orchestrator.LLMOrchestrator"),
        ("swarm", "incident_agent.agents.swarm.HierarchicalSwarm"),
        ("debate", "incident_agent.agents.llm_debate.DebatedOrchestrator"),
        ("swarm-debate", "incident_agent.agents.swarm.HierarchicalSwarm"),
    ],
)
def test_diagnose_selects_each_llm_agent_and_passes_risk_guard(monkeypatch, capsys, mode, target) -> None:
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    seen: dict[str, object] = {}

    class Out:
        def format_report(self) -> str:
            return f"report-{mode}"

    class Fake:
        def __init__(self, client, risk_guard=False, debate=False) -> None:
            seen["guard"] = risk_guard
            seen["debate"] = debate

        def run(self, incident_id: str) -> Out:
            return Out()

    monkeypatch.setattr("incident_agent.llm.build_client", lambda: "c")
    monkeypatch.setattr(target, Fake)
    assert cli._cmd_diagnose(["INC-001", "--agent", mode, "--risk-guard"]) == 0
    assert capsys.readouterr().out == f"report-{mode}\n"
    assert seen["guard"] is True
    assert seen.get("debate", False) is (mode == "swarm-debate")


def test_diagnose_legacy_agent_needs_no_llm(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    assert cli._cmd_diagnose(["INC-001", "--agent", "legacy"]) == 0
    assert capsys.readouterr().out.strip()
