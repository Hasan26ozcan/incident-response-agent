# ruff: noqa: E501
"""Lab scenarios: what to inject, and the ground truth a correct diagnosis must reach (same fields as eval/gold)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    category: str
    target: str  # service the fault is injected into: "checkout-api" or "payments-stub"
    fault: str | None  # app fault name, or None (false alarm / load only)
    surge: float  # load multiplier during the fault window (1.0 = unchanged)
    root_cause: str
    evidence: tuple[str, ...]
    remediation: str
    risk_tier: str
    risk_rationale: str
    is_false_alarm: bool = False
    deploy: tuple[str, str] | None = None  # (version, description) deployed at injection time
    broken_deploy: bool = False

    def gold(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "evidence": list(self.evidence),
            "recommended_remediation": self.remediation,
            "remediation_risk_tier": self.risk_tier,
            "risk_rationale": self.risk_rationale,
            "incident_id": self.id,
            "title": self.title,
            "category": self.category,
            "difficulty": "lab",
            "is_false_alarm": self.is_false_alarm,
            "recurrence_of": None,
            "distractor_note": None,
            "primary_stage_use": ["21b"],
            "source": "fault-lab (real processes)",
        }


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in (
        Scenario(
            "LAB-001",
            "cpu runaway retry loop (live)",
            "cpu_exhaustion",
            "checkout-api",
            "cpu_loop",
            1.0,
            "An unbounded retry loop in checkout-api's handler burns CPU on every request and starves the worker threads.",
            (
                "metric:cpu_percent rises to saturation when the fault starts",
                "metric:latency_p99_ms rises with it",
                "log: repeated 'possible unbounded loop detected in retry handler' from checkout-api",
            ),
            "Restart the affected checkout-api instances to shed the stuck work, then patch the retry handler to cap attempts.",
            "high",
            "Restarting a production service is an explicit high-risk action.",
        ),
        Scenario(
            "LAB-002",
            "memory leak heap exhaustion (live)",
            "memory_leak",
            "checkout-api",
            "memory_leak",
            1.0,
            "checkout-api retains memory on every request; the heap grows until allocations fail with OutOfMemory errors.",
            (
                "metric:mem_mb grows steadily after the fault starts",
                "log: 'heap usage ... approaching limit' warnings then OutOfMemory errors",
                "metric:error_rate_percent rises once the heap limit is hit",
            ),
            "Restart checkout-api to reclaim memory and roll back or fix the change that retains per-request objects.",
            "high",
            "Restarting or rolling back a production service is high risk.",
        ),
        Scenario(
            "LAB-003",
            "dependency timeout cascade (live)",
            "cascading_failure",
            "payments-stub",
            "slow",
            1.0,
            "payments-stub became slow (responses take seconds), so checkout-api calls time out and return 504s.",
            (
                "log: checkout-api 'upstream payments-stub timeout after 1000ms'",
                "metric:latency_p99_ms of checkout-api jumps to the timeout",
                "metric:error_rate_percent rises on checkout-api while its own CPU and memory stay normal",
            ),
            "Fix or fail over payments-stub; meanwhile enable a circuit breaker or fallback in checkout-api. Do not restart checkout-api.",
            "medium",
            "The fix is in a dependency; changing checkout-api configuration is medium risk.",
        ),
        Scenario(
            "LAB-004",
            "bad deploy missing config (live)",
            "bad_deploy",
            "checkout-api",
            "bad_config",
            1.0,
            "Deploy 2.4.1 of checkout-api shipped without the PAYMENT_API_URL setting, so every request raises a KeyError.",
            (
                "deploy: version 2.4.1 deployed minutes before the errors began",
                "log: repeated \"KeyError: 'PAYMENT_API_URL' missing in config\"",
                "metric:error_rate_percent jumps to ~100% right after the deploy",
            ),
            "Roll back checkout-api to 2.3.0, then re-deploy 2.4.x with the PAYMENT_API_URL setting present.",
            "high",
            "Rolling back a production deploy is a high-risk action.",
            deploy=("2.4.1", "Refactor payment client configuration"),
            broken_deploy=True,
        ),
        Scenario(
            "LAB-005",
            "false alarm approved load test (live)",
            "false_alarm_loadtest",
            "checkout-api",
            None,
            5.0,
            "Not an incident: a planned load test raised traffic fivefold; latency stays within normal bounds and there are no errors.",
            (
                "metric:rps is several times baseline",
                "log: no errors or warnings",
                "metric:error_rate_percent stays at 0",
            ),
            "No remediation needed. Close the alert and silence it for approved load-test windows.",
            "low",
            "No action is taken.",
            is_false_alarm=True,
        ),
    )
}
