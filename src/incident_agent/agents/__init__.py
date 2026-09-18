"""Agent implementations.

Populated starting Stage 3 (single-agent ReAct skeleton), extended through
Stage 9 (multi-agent orchestration, debate, Tree-of-Thought). See
ROADMAP.md Phase B and Phase C, and openspec/agent-responsibility-matrix.md
for what each agent introduced here is responsible for and what risk tier
its actions fall under.

Stage 4: All agent outputs are Pydantic-validated objects —
see incident_agent.schemas for the canonical schema definitions.
"""

from incident_agent.agents.react_agent import ReActAgent
from incident_agent.schemas.diagnosis import Diagnosis

__all__ = ["ReActAgent", "Diagnosis"]
