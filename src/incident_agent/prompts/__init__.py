"""Prompt library for the incident diagnosis agent.

Stage 4 establishes disciplined system-prompt templating and
few-shot examples. The prompt library provides:
  - SYSTEM_PROMPT_TEMPLATE: the base system prompt with role,
    constraints, and output format specification
  - FEW_SHOT_EXAMPLES: curated examples of incident diagnoses
  - build_prompt(): assembles a complete prompt from template,
    few-shot examples, and incident context

All prompt output is structured to be consumed by a JSON-mode
LLM, producing output that validates against the Pydantic
schemas in `incident_agent.schemas`.
"""

from __future__ import annotations

from incident_agent.prompts.builder import build_prompt
from incident_agent.prompts.few_shot import FEW_SHOT_EXAMPLES
from incident_agent.prompts.system_prompt import SYSTEM_PROMPT_TEMPLATE

__all__ = [
    "SYSTEM_PROMPT_TEMPLATE",
    "FEW_SHOT_EXAMPLES",
    "build_prompt",
]
