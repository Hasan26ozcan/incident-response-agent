"""Few-shot examples for the incident diagnosis agent prompt.

Each example shows a realistic incident diagnosis output that
conforms to the Pydantic Diagnosis schema. These examples are
inserted into the system prompt so the LLM has concrete
templates to follow (Stage 4: disciplined few-shot examples).

The examples cover diverse categories to demonstrate the full
range of root causes, confidence levels, and risk tiers.
"""

from __future__ import annotations

from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep, RiskTier
from incident_agent.schemas.diagnosis import Diagnosis


def _reasoning_step(step_number: int, description: str, evidence_refs: list[str]) -> ReasoningStep:
    """Build a reasoning-step object matching the ReasoningStep schema."""
    return ReasoningStep(step_number=step_number, description=description, evidence_refs=evidence_refs)


def _cpu_exhaustion_example() -> Diagnosis:
    """Example: CPU exhaustion from unbounded retry loop."""
    return Diagnosis(
        agent_type="single_agent_react",
        incident_id="INC-001",
        root_cause=(
            "An unbounded retry loop in the payment-confirmation "
            "handler pins a worker thread at 100 percent CPU per "
            "affected request, starving the thread pool."
        ),
        confidence=0.92,
        evidence=[
            EvidenceItem(
                source_type=EvidenceType.LOG_ENTRY,
                source="log:ERR-0042",
                detail="ERROR at 2026-03-01T08:01:12 Unbounded retry loop detected in payment-confirmation handler - thread pool at 98 percent utilization",
                timestamp="2026-03-01T08:01:12Z",
                confidence_weight=0.95,
            ),
            EvidenceItem(
                source_type=EvidenceType.METRIC_ANOMALY,
                source="metric:cpu_percent",
                detail="METRIC cpu_percent anomaly at 2026-03-01T08:01:00 - value 99.2 percent baseline avg 12.4 percent threshold 35.0 percent",
                timestamp="2026-03-01T08:01:00Z",
                confidence_weight=0.90,
            ),
        ],
        affected_service="checkout-api",
        category="cpu_exhaustion",
        reasoning_steps=[
            _reasoning_step(1, "Observed 47 ERROR log entries for checkout-api", ["log:ERR-0042"]),
            _reasoning_step(
                2, "Identified error pattern unbounded retry loop in payment-confirmation handler", ["log:ERR-0042"]
            ),
            _reasoning_step(
                3, "Detected CPU anomaly at 99.2 percent correlated with error spike timestamp", ["metric:cpu_percent"]
            ),
            _reasoning_step(4, "Correlated deploy d-20260228-01 with incident window", []),
        ],
        recommendation=(
            "Restart affected pods to shed stuck threads, then patch the retry handler to cap retry attempts."
        ),
        risk_tier=RiskTier.HIGH,
    )


def _db_connection_example() -> Diagnosis:
    """Example: Database connection pool exhaustion."""
    return Diagnosis(
        agent_type="single_agent_react",
        incident_id="INC-003",
        root_cause=(
            "Database connection pool exhaustion due to leaked connections "
            "in the request handler - connections opened in the middleware "
            "are never closed on error paths."
        ),
        confidence=0.87,
        evidence=[
            EvidenceItem(
                source_type=EvidenceType.LOG_ENTRY,
                source="log:ERR-0107",
                detail="ERROR at 2026-03-01T14:22:05 Connection pool exhausted - max 50 connections, 49 in use",
                timestamp="2026-03-01T14:22:05Z",
                confidence_weight=0.93,
            ),
            EvidenceItem(
                source_type=EvidenceType.DEPLOY,
                source="deploy:d-20260301-005",
                detail="DEPLOY d-20260301-005 deploy-api-gateway v1.8.2 applied before incident window",
                timestamp="2026-03-01T12:00:00Z",
                confidence_weight=0.60,
            ),
        ],
        affected_service="api-gateway",
        category="db_connection",
        reasoning_steps=[
            _reasoning_step(1, "Observed 23 ERROR log entries for api-gateway", []),
            _reasoning_step(2, "Identified error pattern connection pool exhaustion on error paths", ["log:ERR-0107"]),
            _reasoning_step(3, "Found deploy d-20260301-005 within the pre-incident window", ["deploy:d-20260301-005"]),
            _reasoning_step(4, "Leaked connections on error paths explain the gradual pool exhaustion", []),
        ],
        recommendation=(
            "Restart affected pods to reset connections, then fix the connection leak in the request handler."
        ),
        risk_tier=RiskTier.MEDIUM,
    )


def _memory_leak_example() -> Diagnosis:
    """Example: Memory leak causing OOM kill."""
    return Diagnosis(
        agent_type="single_agent_react",
        incident_id="INC-005",
        root_cause=(
            "Memory leak causing OOM kill - objects retained across request "
            "boundaries in the session cache prevent garbage collection."
        ),
        confidence=0.89,
        evidence=[
            EvidenceItem(
                source_type=EvidenceType.LOG_ENTRY,
                source="log:ERR-0201",
                detail="ERROR at 2026-03-02T03:15:33 OutOfMemoryError - Java heap space exhausted, session cache grew to 2.4GB",
                timestamp="2026-03-02T03:15:33Z",
                confidence_weight=0.94,
            ),
            EvidenceItem(
                source_type=EvidenceType.METRIC_ANOMALY,
                source="metric:memory_usage",
                detail="METRIC memory_usage anomaly at 2026-03-02T03:15:00 - value 98.7 percent baseline 45.2 percent",
                timestamp="2026-03-02T03:15:00Z",
                confidence_weight=0.91,
            ),
        ],
        affected_service="session-service",
        category="memory_leak",
        reasoning_steps=[
            _reasoning_step(1, "Observed OOM errors in session-service logs", ["log:ERR-0201"]),
            _reasoning_step(
                2, "Detected memory anomaly at 98.7 percent correlated with OOM timestamp", ["metric:memory_usage"]
            ),
            _reasoning_step(3, "Session cache growth pattern indicates unbounded retention", []),
            _reasoning_step(4, "No relevant deploy changes before incident - points to code defect", []),
        ],
        recommendation=(
            "Restart affected pods to reclaim memory, then fix the object retention bug in the session cache."
        ),
        risk_tier=RiskTier.HIGH,
    )


# Each example is a pre-built Diagnosis that serializes to
# the JSON structure the LLM should produce.
FEW_SHOT_EXAMPLES: list[dict] = [
    _cpu_exhaustion_example().model_dump(),
    _db_connection_example().model_dump(),
    _memory_leak_example().model_dump(),
]
