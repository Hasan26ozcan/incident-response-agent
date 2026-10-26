"""Allow-listed diagnostic scripts for the sandbox MCP server.

Not a shell: a fixed registry of named, parameter-validated, read-only analyses over one incident's data. Each run is
a separate Python process (isolated mode, minimal environment, no secrets, killed on timeout, output capped, memory
and CPU limits on POSIX). Arbitrary command execution is Stage 16's job; nothing here accepts code or a command line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_agent.tools import read_logs, read_metrics
from incident_agent.tools.toolbox import MAX_OUTPUT_CHARS, ToolError, _describe

SRC_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT_S = 10.0
CPU_LIMIT_S = 20
MEMORY_LIMIT_BYTES = 1_000_000_000


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TimelineArgs(_Args):
    bucket_minutes: int = Field(default=5, ge=1, le=60)
    levels: list[str] = Field(default_factory=lambda: ["ERROR", "WARN"], max_length=4)


class _FirstSeenArgs(_Args):
    pattern: str = Field(min_length=1, max_length=120)


class _MetricArgs(_Args):
    metric: str = Field(min_length=1, max_length=80)


class _NoArgs(_Args):
    pass


def _minute(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def error_timeline(incident_id: str, args: _TimelineArgs) -> str:
    logs = [e for e in read_logs(incident_id) if e.level in {lv.upper() for lv in args.levels}]
    if not logs:
        return "no log lines at the requested levels"
    start = min(_minute(e.timestamp) for e in logs)
    buckets: Counter[tuple[int, str]] = Counter()
    for e in logs:
        buckets[(int((_minute(e.timestamp) - start).total_seconds() // 60 // args.bucket_minutes), e.service)] += 1
    rows = [f"+{b * args.bucket_minutes:>3}m {svc}: {n}" for (b, svc), n in sorted(buckets.items())]
    return f"{len(logs)} lines from {start.isoformat()}, {args.bucket_minutes}-minute buckets:\n" + "\n".join(rows[:60])


def first_seen(incident_id: str, args: _FirstSeenArgs) -> str:
    needle = args.pattern.lower()
    hits = [e for e in read_logs(incident_id) if needle in e.message.lower()]
    if not hits:
        return f"no log lines contain {args.pattern!r}"
    return (
        f"{len(hits)} lines contain {args.pattern!r}; first {hits[0].timestamp} [{hits[0].service}] {hits[0].level}, "
        f"last {hits[-1].timestamp}"
    )


def service_breakdown(incident_id: str, args: _NoArgs) -> str:
    logs = read_logs(incident_id)
    bad = Counter(e.service for e in logs if e.level in ("ERROR", "WARN"))
    total = sum(bad.values())
    if not total:
        return "no ERROR or WARN lines"
    return "\n".join(f"{svc}: {n} ({n / total:.0%} of {total} ERROR/WARN lines)" for svc, n in bad.most_common())


def metric_vs_errors(incident_id: str, args: _MetricArgs) -> str:
    series = read_metrics(incident_id).get(args.metric)
    if not series:
        raise ToolError("run_diagnostic", f"unknown metric {args.metric!r}", bad_args=True)
    base = [p["value"] for p in series[: max(1, len(series) // 5)]]
    avg = sum(base) / len(base)
    anomaly = next((p["ts"] for p in series if p["value"] > avg * 2 + 1e-9), None)
    errors = [e for e in read_logs(incident_id) if e.level == "ERROR"]
    first_error = errors[0].timestamp if errors else None
    if anomaly and first_error:
        lead = (_minute(first_error) - _minute(anomaly)).total_seconds() / 60
        relation = f"first ERROR is {abs(lead):.0f} min {'after' if lead >= 0 else 'before'} the metric anomaly"
    else:
        relation = "no anomaly or no ERROR lines to compare"
    return f"{args.metric}: baseline_avg={avg:.2f} anomaly_start={anomaly} first_error={first_error}; {relation}"


SCRIPTS: dict[str, tuple[type[_Args], Callable[[str, Any], str], str]] = {
    "error_timeline": (_TimelineArgs, error_timeline, "ERROR/WARN counts per time bucket and service."),
    "first_seen": (_FirstSeenArgs, first_seen, "First and last occurrence and count of a log message substring."),
    "service_breakdown": (_NoArgs, service_breakdown, "Which services produce the ERROR/WARN lines."),
    "metric_vs_errors": (_MetricArgs, metric_vs_errors, "Metric anomaly start versus the first ERROR line."),
}


def describe_scripts() -> str:
    lines = []
    for name, (model, _, doc) in sorted(SCRIPTS.items()):
        params = ", ".join(
            f"{k}: {getattr(v.annotation, '__name__', str(v.annotation))}" for k, v in model.model_fields.items()
        )
        params = params or "no arguments"
        lines.append(f"{name} ({params}) - {doc}")
    return "\n".join(lines)


def _child_env() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG")  # nothing that could hold an API key
    return {k: os.environ[k] for k in keep if k in os.environ}


def _limits() -> None:  # pragma: no cover - runs in the child after fork, POSIX only
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_S, CPU_LIMIT_S))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


_CHILD = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from incident_agent.mcp_servers.diagnostics import child_main; child_main()"
)


def run_diagnostic(
    incident_id: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_output: int = MAX_OUTPUT_CHARS,
) -> str:
    if name not in SCRIPTS:
        raise ToolError(
            "run_diagnostic", f"unknown script {name!r}; available: {', '.join(sorted(SCRIPTS))}", bad_args=True
        )
    if arguments is not None and not isinstance(arguments, dict):
        raise ToolError("run_diagnostic", "arguments must be a JSON object", bad_args=True)
    try:
        validated = SCRIPTS[name][0].model_validate(arguments or {}).model_dump()
    except ValidationError as exc:
        raise ToolError("run_diagnostic", f"bad arguments for {name} ({_describe(exc)})", bad_args=True) from exc
    payload = json.dumps({"incident_id": incident_id, "name": name, "arguments": validated})
    try:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter, fixed code, validated JSON on stdin
            [sys.executable, "-I", "-c", _CHILD, str(SRC_ROOT)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
            cwd=str(SRC_ROOT),
            preexec_fn=_limits if os.name == "posix" else None,  # noqa: PLW1509
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError("run_diagnostic", f"{name} exceeded the {timeout:g}s limit and was killed") from exc
    except OSError as exc:
        raise ToolError("run_diagnostic", f"could not start the sandbox process: {exc}") from exc
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(
            "run_diagnostic", f"{name} failed (exit {proc.returncode}): {proc.stderr.strip()[-300:]}"
        ) from exc
    if not result.get("ok"):
        raise ToolError("run_diagnostic", str(result.get("error", "failed")), bad_args=bool(result.get("bad_args")))
    text = str(result["output"])
    return text if len(text) <= max_output else text[:max_output] + "\n...[truncated]"


def child_main() -> None:  # pragma: no cover - executed in the child process
    request = json.loads(sys.stdin.read())
    iid = request["incident_id"]
    if not re.fullmatch(r"INC-\d{3}", iid):
        print(json.dumps({"ok": False, "error": "invalid incident id"}))
        return
    model, fn, _ = SCRIPTS[request["name"]]
    try:
        print(json.dumps({"ok": True, "output": fn(iid, model.model_validate(request["arguments"]))}))
    except ToolError as exc:
        print(json.dumps({"ok": False, "error": exc.reason, "bad_args": exc.bad_args}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
