"""Incident Response Agent — agentic incident triage and diagnosis.

This package is built up incrementally across the 23-stage roadmap in
ROADMAP.md. At Stage 3, the single-agent ReAct loop is available via
`incident_agent.agents.react_agent`.
"""

__version__ = "0.1.0"

from incident_agent.agents.react_agent import Diagnosis, ReActAgent

__all__ = ["__version__", "Diagnosis", "ReActAgent"]
