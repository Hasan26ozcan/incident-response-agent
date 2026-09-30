"""Episodic Memory schemas — Stage 12."""
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional


class PostMortem(BaseModel):
    incident_id: str
    root_cause: str
    resolution: str
    timestamp: str
    embedding_text: str
    tags: list[str] = Field(default_factory=list)


class ReflectionFeedback(BaseModel):
    misdiagnosis_type: Optional[str] = None
    confidence_delta: float = 0.0
    updated_strategy: Optional[str] = None
    correct: bool = True


class MemoryRecord(BaseModel):
    post_mortem: PostMortem
    reflection: ReflectionFeedback
    accuracy_before: float = 0.0
    accuracy_after: float = 0.0
