"""Runtime tracing (Stage 21): spans, a tracer and local sinks. Trace = what happened in one run, in order, with timing.

Local-first and dependency-free: spans are plain dataclasses written as JSON lines. Exporters to LangSmith, Phoenix
(OpenTelemetry) or W&B are optional adapters over the same spans (``exporters.py``). Telemetry never changes behaviour:
a failing sink is counted and skipped, never raised into the agent, and nothing credential-shaped leaves the process
(``redact``). Message content is *not* recorded by default (only hashes and sizes).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from incident_agent.observe.redact import clean_value, redact

KINDS = ("run", "agent", "llm", "tool", "step", "gate", "sandbox", "span")
_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar("observe_current_span", default=None)


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str = "span"
    start: float = 0.0
    end: float = 0.0
    status: str = "ok"  # ok | error
    error: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Span:
        return cls(
            trace_id=str(row["trace_id"]),
            span_id=str(row["span_id"]),
            parent_id=row.get("parent_id"),
            name=str(row["name"]),
            kind=str(row.get("kind", "span")),
            start=float(row.get("start", 0.0)),
            end=float(row.get("end", 0.0)),
            status=str(row.get("status", "ok")),
            error=row.get("error"),
            attrs=dict(row.get("attrs") or {}),
        )


class Sink(Protocol):
    def emit(self, span: Span) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    def emit(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span)


class JsonlSink:
    """Append one JSON line per finished span (atomic enough for single lines; safe across threads)."""

    def __init__(self, path: Path | str, *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self._lock = threading.Lock()

    def emit(self, span: Span) -> None:
        line = json.dumps(span.to_dict(), sort_keys=True, default=str) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                if self.fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)


def read_spans(path: Path | str) -> list[Span]:
    """Read a JSONL trace file; torn or foreign lines are skipped (returned count via ``skipped``-free API)."""
    spans: list[Span] = []
    p = Path(path)
    if not p.exists():
        return spans
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            spans.append(Span.from_dict(json.loads(line)))
        except (ValueError, KeyError, TypeError):
            continue
    return spans


class Tracer:
    def __init__(
        self,
        sinks: list[Sink] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        capture_content: bool = False,
        service: str = "incident-agent",
    ) -> None:
        self.sinks: list[Sink] = list(sinks or [])
        self.clock = clock
        self.capture_content = capture_content
        self.service = service
        self.dropped = 0  # spans a sink failed to take: telemetry must never break the run

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def _emit(self, span: Span) -> None:
        for sink in self.sinks:
            try:
                sink.emit(span)
            except Exception:  # noqa: BLE001 - observability is best-effort by design
                self.dropped += 1

    @contextlib.contextmanager
    def span(self, name: str, kind: str = "span", **attrs: Any) -> Iterator[Span]:
        parent = _current.get()
        sp = Span(
            trace_id=parent.trace_id if parent else secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            parent_id=parent.span_id if parent else None,
            name=str(name)[:128],
            kind=kind if kind in KINDS else "span",
            start=self.clock(),
            attrs={"service": self.service},
        )
        sp.attrs.update({str(k): clean_value(v) for k, v in attrs.items()})
        token = _current.set(sp)
        try:
            yield sp
        except BaseException as exc:
            sp.status = "error"
            sp.error = redact(f"{type(exc).__name__}: {exc}")[:500]
            raise
        finally:
            _current.reset(token)
            sp.end = self.clock()
            sp.attrs = {k: clean_value(v) for k, v in sp.attrs.items()}
            self._emit(sp)

    def event(self, name: str, kind: str = "span", **attrs: Any) -> Span:
        """A zero-length span (something that happened, not something that took time)."""
        with self.span(name, kind, **attrs) as sp:
            return sp


def current_span() -> Span | None:
    return _current.get()
