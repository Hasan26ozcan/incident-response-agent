"""Allow-listed sandbox commands (Stage 16): the registry, strict argument parsing and the implementations.

This module is **standard library only** and is used twice: the parent process imports it to parse and classify a
command line, and the sandbox child executes this very file's source (``python -I -S -B -c <source>``), so the child
needs no packages and no access to the project tree. The child re-validates everything it receives and trusts nothing.

Design rules:

* there is no shell and no free-form argument: every command has a typed, bounded argument list;
* remediation commands act on a *simulated* cluster derived from the incident data (real remediation needs network
  and credentials, so it is approval-gated work for Stage 18, not something this stage fakes);
* a mutating command with ``--dry-run`` only describes what it would do (low risk); without it the command is high
  risk and the parent sets ``apply`` only after a matching human approval.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

LOW, HIGH = "low", "high"
MAX_OUTPUT = 4000
MAX_STDIN = 2_000_000
LEVELS = ("DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL")
SERVICE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
METRIC_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z._-]{0,31}")
TEXT_RE = re.compile(r"[^\x00-\x1f\x7f]+")

# isolation guarantees (names shared with backends.py)
BASIC = frozenset({"env", "time", "output"})
ISOLATED = BASIC | {"network", "filesystem", "processes"}


class CommandError(ValueError):
    """A command line or request that is refused; the message is safe to show to the agent."""


@dataclass(frozen=True)
class Arg:
    name: str
    kind: str  # text | int | flag | choice
    positional: bool = False
    required: bool = False
    pattern: re.Pattern[str] | None = None
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    max_len: int = 100
    default: Any = None


@dataclass(frozen=True)
class Spec:
    name: str
    summary: str
    args: tuple[Arg, ...] = ()
    mutating: bool = False
    requires: frozenset[str] = field(default_factory=lambda: BASIC)

    def tier(self, args: dict[str, Any]) -> str:
        return HIGH if self.mutating and not args.get("dry_run") else LOW

    def usage(self) -> str:
        parts = [self.name]
        for a in self.args:
            if a.positional:
                parts.append(a.name.upper() if a.required else f"[{a.name.upper()}]")
            elif a.kind == "flag":
                parts.append(f"[--{a.name.replace('_', '-')}]")
            else:
                text = f"--{a.name.replace('_', '-')} {a.name.upper()}"
                parts.append(text if a.required else f"[{text}]")
        return " ".join(parts)


def _service(required: bool = True) -> Arg:
    return Arg("service", "text", positional=True, required=required, pattern=SERVICE_RE, max_len=63)


def _dry() -> Arg:
    return Arg("dry_run", "flag")


_SPEC_LIST = (
    Spec("list-services", "list the services in this incident"),
    Spec("cluster-status", "replicas, version and health of one service", (_service(),)),
    Spec(
        "logs-grep",
        "case-insensitive substring search in the incident logs",
        (
            Arg("pattern", "text", positional=True, required=True, pattern=TEXT_RE, max_len=100),
            Arg("level", "choice", choices=LEVELS),
            Arg("service", "text", pattern=SERVICE_RE, max_len=63),
            Arg("max", "int", minimum=1, maximum=50, default=10),
        ),
    ),
    Spec(
        "log-stats",
        "log counts per level and time bucket",
        (Arg("bucket_minutes", "int", minimum=1, maximum=60, default=5),),
    ),
    Spec(
        "metric-stats",
        "min / mean / max of one metric",
        (Arg("metric", "text", positional=True, required=True, pattern=METRIC_RE, max_len=63),),
    ),
    Spec(
        "restart-service",
        "restart a service (simulated)",
        (_service(), Arg("reason", "text", pattern=TEXT_RE, max_len=200), _dry()),
        mutating=True,
        requires=ISOLATED,
    ),
    Spec(
        "scale-service",
        "change the replica count (simulated)",
        (_service(), Arg("replicas", "int", required=True, minimum=1, maximum=20), _dry()),
        mutating=True,
        requires=ISOLATED,
    ),
    Spec(
        "rollback-deploy",
        "roll a service back to an earlier version (simulated)",
        (_service(), Arg("to", "text", pattern=VERSION_RE, max_len=32), _dry()),
        mutating=True,
        requires=ISOLATED,
    ),
    Spec("flush-cache", "flush a service's cache (simulated)", (_service(), _dry()), mutating=True, requires=ISOLATED),
)
SPECS: dict[str, Spec] = {s.name: s for s in _SPEC_LIST}


def _check_value(arg: Arg, raw: Any) -> Any:
    label = arg.name.replace("_", "-")
    if arg.kind == "flag":
        if raw is not True:
            raise CommandError(f"--{label} takes no value")
        return True
    if arg.kind == "int":
        is_int = isinstance(raw, int) and not isinstance(raw, bool)
        if not (is_int or (isinstance(raw, str) and re.fullmatch(r"[0-9]{1,6}", raw))):
            raise CommandError(f"{label} must be a whole number")
        value = int(raw)
        if not arg.minimum <= value <= arg.maximum:
            raise CommandError(f"{label} must be between {arg.minimum} and {arg.maximum}")
        return value
    if not isinstance(raw, str):
        raise CommandError(f"{label} must be text")
    if len(raw) > arg.max_len:
        raise CommandError(f"{label} is longer than {arg.max_len} characters")
    if arg.kind == "choice":
        if raw.upper() not in arg.choices:
            raise CommandError(f"{label} must be one of {', '.join(arg.choices)}")
        return raw.upper()
    if not raw or not (arg.pattern or TEXT_RE).fullmatch(raw):
        raise CommandError(f"{label} has characters that are not allowed")
    return raw


def _finish(spec: Spec, found: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in spec.args:
        if a.name in found:
            out[a.name] = _check_value(a, found[a.name])
        elif a.required:
            raise CommandError(f"missing required argument {a.name.upper()}; usage: {spec.usage()}")
        elif a.default is not None:
            out[a.name] = a.default
        elif a.kind == "flag":
            out[a.name] = False
    return out


def parse_tokens(spec: Spec, tokens: list[str]) -> dict[str, Any]:
    """Turn the tokens after the command name into validated arguments, or raise CommandError."""
    options = {a.name.replace("_", "-"): a for a in spec.args if not a.positional}
    positionals = [a for a in spec.args if a.positional]
    found: dict[str, Any] = {}
    pos = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            raise CommandError("'--' is not supported")
        if tok.startswith("--"):
            if "=" in tok:
                raise CommandError("write options as '--name value', not '--name=value'")
            arg = options.get(tok[2:])
            if arg is None:
                raise CommandError(f"unknown option {tok[:40]!r}; usage: {spec.usage()}")
            if arg.name in found:
                raise CommandError(f"--{tok[2:]} was given twice")
            if arg.kind == "flag":
                found[arg.name] = True
            else:
                i += 1
                if i >= len(tokens) or tokens[i].startswith("--"):
                    raise CommandError(f"--{tok[2:]} needs a value")
                found[arg.name] = tokens[i]
        elif tok.startswith("-"):
            raise CommandError(f"argument {tok[:40]!r} starts with '-'; use long options such as --max")
        else:
            if pos >= len(positionals):
                raise CommandError(f"unexpected extra argument {tok[:40]!r}; usage: {spec.usage()}")
            found[positionals[pos].name] = tok
            pos += 1
        i += 1
    return _finish(spec, found)


def validate_args(spec: Spec, raw: Any) -> dict[str, Any]:
    """Re-check an already parsed argument dict (the child does not trust its parent)."""
    if not isinstance(raw, dict):
        raise CommandError("arguments must be an object")
    known = {a.name for a in spec.args}
    extra = set(raw) - known
    if extra:
        raise CommandError(f"unknown argument(s): {', '.join(sorted(str(k)[:30] for k in extra))}")
    return _finish(spec, {k: v for k, v in raw.items() if v is not None and v is not False})


# --- simulated cluster -----------------------------------------------------------------------------------------


def _deploys(data: dict[str, Any], service: str) -> list[dict[str, Any]]:
    rows = [d for d in data.get("deploys", []) if isinstance(d, dict) and d.get("service") == service]
    return sorted(rows, key=lambda d: str(d.get("timestamp")))


def cluster_state(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A deterministic fake cluster: 3 replicas per service, version from the latest deploy, health from ERROR logs."""
    errors = Counter(e.get("service") for e in data.get("logs", []) if str(e.get("level")).upper() == "ERROR")
    state: dict[str, dict[str, Any]] = {}
    for svc in sorted(str(s) for s in data.get("meta", {}).get("services", [])):
        versions = _versions(data, svc)
        version = versions[-1] if versions else "unknown"
        health = "degraded" if errors[svc] else "healthy"
        state[svc] = {"replicas": 3, "version": version, "health": health, "cache": "warm"}
    return state


def _versions(data: dict[str, Any], service: str) -> list[str]:
    return [str(d["version"]) for d in _deploys(data, service) if d.get("version") and d["version"] != "n/a"]


def _need(state: dict[str, dict[str, Any]], service: str) -> dict[str, Any]:
    if service not in state:
        raise CommandError(f"unknown service {service!r}; in this incident: {', '.join(state) or 'none'}")
    return state[service]


def _ok(output: str, changed: bool = False, before: Any = None, after: Any = None) -> dict[str, Any]:
    return {"ok": True, "output": output[:MAX_OUTPUT], "changed": changed, "before": before, "after": after}


def _list_services(args: dict[str, Any], data: dict[str, Any], apply: bool) -> dict[str, Any]:
    state = cluster_state(data)
    return _ok("\n".join(f"{n}  {s['health']}" for n, s in state.items()) or "no services")


def _cluster_status(args: dict[str, Any], data: dict[str, Any], apply: bool) -> dict[str, Any]:
    s = _need(cluster_state(data), args["service"])
    return _ok(f"{args['service']}: replicas={s['replicas']} version={s['version']} health={s['health']}")


def _logs_grep(args: dict[str, Any], data: dict[str, Any], apply: bool) -> dict[str, Any]:
    needle = args["pattern"].lower()
    hits = [
        e
        for e in data.get("logs", [])
        if needle in str(e.get("message", "")).lower()
        and (not args.get("level") or str(e.get("level")).upper() == args["level"])
        and (not args.get("service") or e.get("service") == args["service"])
    ]
    if not hits:
        return _ok(f"no log lines contain {args['pattern']!r} with these filters")
    lines = [f"{e['timestamp']} {e['service']} {e['level']} {e['message']}" for e in hits[: args["max"]]]
    return _ok(f"{len(hits)} match(es), showing {len(lines)}:\n" + "\n".join(lines))


def _log_stats(args: dict[str, Any], data: dict[str, Any], apply: bool) -> dict[str, Any]:
    logs = data.get("logs", [])
    if not logs:
        return _ok("no logs")
    levels = Counter(str(e.get("level")) for e in logs)
    buckets: Counter[str] = Counter()
    width = args["bucket_minutes"]
    for e in logs:
        ts = str(e.get("timestamp"))
        try:
            minute = int(ts[14:16])
        except ValueError:
            continue
        buckets[f"{ts[:13]}:{minute - minute % width:02d}"] += 1
    top = ", ".join(f"{k} x{v}" for k, v in sorted(buckets.items())[:20])
    return _ok(f"levels: {dict(sorted(levels.items()))}\nbuckets ({width} min): {top}")


def _metric_stats(args: dict[str, Any], data: dict[str, Any], apply: bool) -> dict[str, Any]:
    series = data.get("metrics", {}).get(args["metric"])
    if not isinstance(series, list) or not series:
        known = ", ".join(sorted(data.get("metrics", {})))
        raise CommandError(f"unknown metric {args['metric']!r}; available: {known or 'none'}")
    values = [float(p["value"]) for p in series if isinstance(p, dict) and "value" in p]
    if not values:
        raise CommandError("metric has no values")
    return _ok(
        f"{args['metric']}: n={len(values)} min={min(values):.2f} "
        f"mean={sum(values) / len(values):.2f} max={max(values):.2f}"
    )


def _plan(name: str, args: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    state = cluster_state(data)
    svc = args["service"]
    before = dict(_need(state, svc))
    after = dict(before)
    if name == "restart-service":
        after["health"] = "healthy"
        why = f" because: {args['reason']}" if args.get("reason") else ""
        what = f"restart {svc} ({before['replicas']} replicas){why}"
    elif name == "scale-service":
        after["replicas"] = args["replicas"]
        what = f"scale {svc} from {before['replicas']} to {args['replicas']} replicas"
    elif name == "rollback-deploy":
        versions = _versions(data, svc)
        target = args.get("to")
        if target is None:
            older = [v for v in versions[:-1] if v != before["version"]]
            if not older:
                raise CommandError(f"no earlier version of {svc} in the deploy history")
            target = older[-1]
        elif target not in versions or target == before["version"]:
            raise CommandError(f"version {target!r} is not an earlier deployed version of {svc}")
        after["version"] = target
        what = f"roll {svc} back from {before['version']} to {target}"
    else:
        after["cache"] = "empty"
        what = f"flush the cache of {svc}"
    return before, after, what


def _mutate(name: str) -> Any:
    def handler(args: dict[str, Any], data: dict[str, Any], apply: bool) -> dict[str, Any]:
        before, after, what = _plan(name, args, data)
        if not apply:
            return _ok(f"[dry-run] would {what}. Nothing was changed.", False, before, after)
        return _ok(f"[simulated] did {what}. no real service was touched.", True, before, after)

    return handler


HANDLERS: dict[str, Any] = {
    "list-services": _list_services,
    "cluster-status": _cluster_status,
    "logs-grep": _logs_grep,
    "log-stats": _log_stats,
    "metric-stats": _metric_stats,
    **{n: _mutate(n) for n, s in SPECS.items() if s.mutating},
}


def execute(request: Any) -> dict[str, Any]:
    """Run one request ``{incident_id, command, args, apply, data}``. Never raises for bad input."""
    try:
        if not isinstance(request, dict):
            raise CommandError("request must be an object")
        spec = SPECS.get(str(request.get("command")))
        if spec is None:
            raise CommandError("unknown command")
        args = validate_args(spec, request.get("args"))
        data = request.get("data")
        if not isinstance(data, dict):
            raise CommandError("incident data is missing")
        apply = request.get("apply") is True and spec.mutating and not args.get("dry_run")
        return HANDLERS[spec.name](args, data, apply)  # type: ignore[no-any-return]
    except CommandError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never leak a traceback or incident data out of the sandbox
        return {"ok": False, "error": f"internal error: {type(exc).__name__}"}


def _child_main() -> None:
    raw = sys.stdin.buffer.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        print(json.dumps({"ok": False, "error": "input too large"}))
        return
    try:
        request = json.loads(raw)
    except ValueError:
        print(json.dumps({"ok": False, "error": "input is not JSON"}))
        return
    print(json.dumps(execute(request)))


if __name__ == "__main__":
    _child_main()
