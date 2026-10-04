"""Self-Improvement / Reflection Agent — Stage 12.

Reflects on a resolved incident, writes a post-mortem,
flags misdiagnoses, and updates strategy.
"""

from __future__ import annotations

from incident_agent.memory import store
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.schemas.memory import MemoryRecord, PostMortem, ReflectionFeedback


class SelfImprovementAgent:
    def reflect_and_improve(
        self,
        incident_id: str,
        diagnosis: Diagnosis,
        gold_root_cause: str,
        accuracy_before: float = 0.0,
    ) -> MemoryRecord:
        correct = diagnosis.root_cause == gold_root_cause
        misdiagnosis = None if correct else f"expected {gold_root_cause}, got {diagnosis.root_cause}"
        updated = None if correct else f"focus on {gold_root_cause} patterns"
        reflection = ReflectionFeedback(
            misdiagnosis_type=misdiagnosis,
            confidence_delta=0.1 if correct else -0.1,
            updated_strategy=updated,
            correct=correct,
        )
        post_mortem = PostMortem(
            incident_id=incident_id,
            root_cause=diagnosis.root_cause,
            resolution="resolved",
            timestamp="2026-09-30T00:00:00Z",
            embedding_text=f"{incident_id} {diagnosis.root_cause}",
            tags=[diagnosis.root_cause.lower().replace(" ", "_")],
        )
        record = MemoryRecord(
            post_mortem=post_mortem,
            reflection=reflection,
            accuracy_before=accuracy_before,
            accuracy_after=1.0 if correct else accuracy_before,
        )
        store.save_memory(record)
        return record
