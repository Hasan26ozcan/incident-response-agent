"""Optional exporters (Stage 21). Trace = runtime observability (LangSmith / Arize Phoenix via OpenTelemetry);
W&B = experiment / model comparison (eval metrics per run). All are opt-in, lazily imported, and never required:
if the package or credentials are missing the exporter reports why and the run continues with the local sink."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from incident_agent.observe.trace import Span


class ExporterUnavailable(RuntimeError):
    pass


class OtelSink:
    """Replays finished spans to any OTLP endpoint (Phoenix: ``phoenix serve`` -> http://localhost:6006/v1/traces)."""

    def __init__(self, endpoint: str = "http://localhost:6006/v1/traces", service: str = "incident-agent") -> None:
        try:
            from opentelemetry import trace as ot
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        except ImportError as exc:
            raise ExporterUnavailable(
                "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http (or arize-phoenix-otel)"
            ) from exc
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        self._tracer = provider.get_tracer("incident_agent.observe")
        self._ot = ot

    def emit(self, span: Span) -> None:
        s = self._tracer.start_span(span.name, start_time=int(span.start * 1e9))
        for k, v in span.attrs.items():
            if isinstance(v, (str, bool, int, float)):
                s.set_attribute(k, v)
        s.set_attribute("span.kind", span.kind)
        if span.status == "error":
            s.set_status(self._ot.Status(self._ot.StatusCode.ERROR, span.error or ""))
        s.end(end_time=int(span.end * 1e9))


class LangSmithSink:
    """LangSmith (free plan: 5k base traces / month). Needs LANGSMITH_API_KEY in the environment (never an argument)."""

    def __init__(self, project: str = "incident-agent") -> None:
        if not os.environ.get("LANGSMITH_API_KEY"):
            raise ExporterUnavailable("LANGSMITH_API_KEY is not set")
        try:
            from langsmith import Client
        except ImportError as exc:
            raise ExporterUnavailable("pip install langsmith") from exc
        self._client = Client()
        self.project = project

    def emit(self, span: Span) -> None:
        import uuid

        def u(x: str) -> uuid.UUID:
            return uuid.UUID(x.ljust(32, "0")[:32])

        run_type = {"llm": "llm", "tool": "tool"}.get(span.kind, "chain")
        self._client.create_run(
            id=u(span.span_id),
            trace_id=u(span.trace_id),
            parent_run_id=u(span.parent_id) if span.parent_id else None,
            name=span.name,
            run_type=run_type,
            project_name=self.project,
            start_time=span.start,
            end_time=span.end,
            error=span.error,
            extra={"metadata": span.attrs},
            inputs={},
            outputs={},
        )


def log_wandb(
    metrics: dict[str, Any], *, project: str = "incident-agent", name: str | None = None, run_dir: Path | str = "wandb"
) -> str:
    """Log one experiment (eval/gate metrics) to W&B. Offline by default; ``wandb sync <dir>`` uploads later."""
    if "WANDB_MODE" not in os.environ:
        os.environ["WANDB_MODE"] = "offline"
    try:
        import wandb
    except ImportError as exc:
        raise ExporterUnavailable("pip install wandb") from exc
    flat = {k: v for k, v in metrics.items() if isinstance(v, (int, float, bool))}
    run = wandb.init(project=project, name=name, dir=str(run_dir), config={"kind": "eval"}, reinit=True)
    try:
        run.log(flat)
        return str(run.dir)
    finally:
        run.finish()
