"""Derive traces from the durable journal (Stage 21): every workflow run already records what happened and when,
so a trace needs no extra instrumentation. One ``run`` span per journal; one ``step`` span per started step
(``wait`` spans for approval waits); failures, retries and crashes are marked."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from incident_agent.observe.trace import Span


def _sid(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def spans_from_events(run_id: str, events: list[dict[str, Any]]) -> list[Span]:
    if not events:
        return []
    trace_id = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    root_id = _sid(run_id, "root")
    first, last = float(events[0].get("ts", 0)), float(events[-1].get("ts", 0))
    status, error = "running", None
    open_steps: dict[Any, dict[str, Any]] = {}
    out: list[Span] = []
    retries = 0
    for ev in events:
        kind, ts = ev.get("kind"), float(ev.get("ts", 0))
        step = ev.get("step")
        if kind == "step_started":
            open_steps[step] = ev
        elif kind in ("step_completed", "step_failed", "wait_timed_out") and step in open_steps:
            st = open_steps.pop(step)
            failed = kind == "step_failed"
            retries += failed and bool(ev.get("retryable"))
            out.append(
                Span(
                    trace_id,
                    _sid(run_id, step, st["seq"]),
                    root_id,
                    str(step),
                    "step",
                    float(st["ts"]),
                    ts,
                    "error" if failed else "ok",
                    ev.get("error") if failed else None,
                    {
                        "attempt": st.get("attempt"),
                        "interrupted_before": bool(st.get("interrupted_before")),
                        "seq": st["seq"],
                    },
                )
            )
        elif kind == "run_completed":
            status = "ok"
        elif kind == "run_failed":
            status, error = "error", str(ev.get("error", ""))[:300]
        elif kind == "run_cancelled":
            status = "cancelled"
    for step, st in open_steps.items():  # started, never finished: crashed, waiting or still running
        out.append(
            Span(
                trace_id,
                _sid(run_id, step, st["seq"]),
                root_id,
                str(step),
                "step",
                float(st["ts"]),
                last,
                "unfinished",
                None,
                {"attempt": st.get("attempt"), "seq": st["seq"]},
            )
        )
    waits = [e for e in events if e.get("kind") in ("wait_started", "signal_received", "wait_timed_out")]
    root = Span(
        trace_id,
        root_id,
        None,
        f"run:{run_id}",
        "run",
        first,
        last,
        "ok" if status == "ok" else ("error" if status == "error" else status),
        error,
        {
            "run_id": run_id,
            "events": len(events),
            "steps": len(out),
            "retries": int(retries),
            "wait_events": len(waits),
        },
    )
    return [root, *sorted(out, key=lambda s: (s.start, s.attrs.get("seq", 0)))]


def spans_from_run_dir(run_dir: Path | str) -> list[Span]:
    from incident_agent.durable.journal import Journal

    d = Path(run_dir)
    return spans_from_events(d.name, Journal(d).events())


def spans_from_root(root: Path | str) -> list[Span]:
    spans: list[Span] = []
    r = Path(root)
    if not r.is_dir():
        return spans
    for p in sorted(r.iterdir()):
        if (p / "journal.jsonl").exists():
            try:
                spans.extend(spans_from_run_dir(p))
            except Exception:  # noqa: BLE001, S112  # nosec B112 - one corrupt run must not hide the others
                continue
    return spans
