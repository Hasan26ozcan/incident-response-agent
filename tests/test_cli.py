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
        def __init__(self, client) -> None:
            assert client == "test-client"

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
