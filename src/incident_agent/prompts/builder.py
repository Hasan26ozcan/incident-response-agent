"""Prompt builder — assembles complete prompts from template, examples, and incident context.

Stage 4 provides the build_prompt() utility that combines
the system prompt template, few-shot examples, and incident-specific
data into a single prompt string ready for LLM consumption.

The output is designed for JSON-mode LLM calls where the
response must validate against the Diagnosis Pydantic schema.
"""

from __future__ import annotations

from incident_agent.prompts.few_shot import FEW_SHOT_EXAMPLES
from incident_agent.prompts.system_prompt import SYSTEM_PROMPT_TEMPLATE


def _format_evidence(evidence: list[dict]) -> str:
    """Format evidence items for the prompt context."""
    if not evidence:
        return "  (none)"
    lines = []
    for e in evidence:
        lines.append(f"  - [{e.get('source_type', 'unknown')}] {e.get('detail', '')}")
    return "\n".join(lines)


def _format_logs(log_entries: list[str]) -> str:
    """Format log entries for the prompt context."""
    if not log_entries:
        return "  (no error logs found)"
    lines = []
    for entry in log_entries[:20]:
        lines.append(f"  {entry}")
    return "\n".join(lines)


def _format_metrics(metric_anomalies: dict[str, list[str]]) -> str:
    """Format metric anomalies for the prompt context."""
    if not metric_anomalies:
        return "  (no metric anomalies detected)"
    lines = []
    for metric, timestamps in metric_anomalies.items():
        lines.append(f"  {metric}: anomaly at {', '.join(timestamps[:3])}")
    return "\n".join(lines)


def _format_deploys(deploys: list[dict]) -> str:
    """Format deploy entries for the prompt context."""
    if not deploys:
        return "  (no deploys found in pre-incident window)"
    lines = []
    for dep in deploys[:5]:
        lines.append(
            f"  - {dep.get('deploy_id', 'unknown')}: {dep.get('description', '')} at {dep.get('timestamp', '')}"
        )
    return "\n".join(lines)


def _format_examples(examples: list[dict] | None = None) -> str:
    """Format few-shot examples for the system prompt."""
    ex = examples or FEW_SHOT_EXAMPLES
    lines = []
    for i, example in enumerate(ex, 1):
        lines.append(f"### Example {i}")
        lines.append(f"Incident: {example.get('incident_id')}")
        lines.append(f"Root Cause: {example.get('root_cause')}")
        lines.append(f"Confidence: {example.get('confidence')}")
        lines.append(f"Risk Tier: {example.get('risk_tier')}")
        lines.append(f"Evidence Count: {len(example.get('evidence', []))}")
        lines.append(f"Recommendation: {example.get('recommendation')}")
        lines.append("")
    return "\n".join(lines)


def build_prompt(
    incident_id: str,
    category: str,
    services: list[str],
    window_start: str,
    window_end: str,
    error_logs: list[str],
    metric_anomalies: dict[str, list[str]],
    deploys: list[dict],
    examples: list[dict] | None = None,
) -> str:
    """Assemble a complete prompt for the incident diagnosis LLM.

    Args:
        incident_id: The incident identifier (e.g. "INC-001").
        category: The incident category.
        services: List of affected services.
        window_start: ISO-8601 start of incident window.
        window_end: ISO-8601 end of incident window.
        error_logs: List of formatted error log strings.
        metric_anomalies: Mapping of metric name -> anomaly timestamps.
        deploys: List of deploy dicts in the pre-incident window.
        examples: Override few-shot examples (defaults to FEW_SHOT_EXAMPLES).

    Returns:
        A complete prompt string ready for JSON-mode LLM invocation.
    """
    example_text = _format_examples(examples or FEW_SHOT_EXAMPLES)
    logs_text = _format_logs(error_logs)
    metrics_text = _format_metrics(metric_anomalies)
    deploys_text = _format_deploys(deploys)
    services_str = ", ".join(services) if services else "unknown"

    return SYSTEM_PROMPT_TEMPLATE.format(
        examples=example_text,
        incident_id=incident_id,
        category=category,
        services=services_str,
        window_start=window_start,
        window_end=window_end,
        error_logs=logs_text,
        metric_anomalies=metrics_text,
        deploys=deploys_text,
    )
