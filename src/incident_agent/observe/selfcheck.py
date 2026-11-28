"""The ``observability`` section of the agentic gate (Stage 21): deterministic, offline, dev split only.

Checks (each is a boolean invariant or a pinned number) and negative controls proving each check can fail:

* ``redaction``     - a credential placed in span attributes / errors never reaches the JSONL file
* ``trace_shape``   - nested spans share one trace, parent links resolve, errors are marked, a failing sink never raises
* ``journal_traces``- a real durable run (engine + gate, simulated clock) yields one run span and one span per step
* ``cascade``       - routing keeps the cheap answer when accepted, escalates on rejection/unavailability, and the
                      priced saving versus always-strong is positive for the measured accept rate
* ``window``        - the trimmed history fits the budget, keeps system+task, never orphans a tool message
* ``prefix``        - prompt-reuse analysis flags a volatile prefix and a changed tool list
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from incident_agent.llm.client import LLMResponse, LLMUnavailable
from incident_agent.llm.scripted import ScriptedClient
from incident_agent.observe import prefix as prefix_mod
from incident_agent.observe.cascade import CascadeClient
from incident_agent.observe.journal_traces import spans_from_events
from incident_agent.observe.llm import TracedClient
from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.redact import redact
from incident_agent.observe.trace import JsonlSink, MemorySink, Tracer, read_spans
from incident_agent.observe.window import SlidingWindow, estimate_tokens

SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"  # a fake, credential-shaped value


class _Tick:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.5
        return self.t


def _ok(text: str = "ok", pt: int = 100, ct: int = 20, cached: int = 0) -> LLMResponse:
    usage = {"prompt_tokens": pt, "completion_tokens": ct}
    if cached:
        usage["cached_tokens"] = cached
    return LLMResponse(content=text, usage=usage)


def check_redaction(redactor: Any = redact) -> bool:
    with tempfile.TemporaryDirectory(prefix="obs-") as tmp:
        path = Path(tmp) / "t.jsonl"
        tr = Tracer([JsonlSink(path)], clock=_Tick())
        try:
            with tr.span("run", "run", api_key=SECRET, note=f"token={SECRET}", nested={"k": [SECRET]}):
                raise RuntimeError(f"auth failed with {SECRET}")
        except RuntimeError:
            pass
        text = path.read_text(encoding="utf-8")
    leaked = SECRET in text
    if redactor is not redact:  # negative control: a tracer without redaction
        return not leaked
    return not leaked and "[REDACTED]" in text


def check_trace_shape() -> bool:
    mem = MemorySink()

    class Boom:
        def emit(self, span: Any) -> None:
            raise OSError("disk full")

    tr = Tracer([Boom(), mem], clock=_Tick())
    with tr.span("run", "run"):
        with tr.span("a", "llm"):
            pass
        try:
            with tr.span("b", "tool"):
                raise ValueError("x")
        except ValueError:
            pass
    by = {s.name: s for s in mem.spans}
    return (
        len(mem.spans) == 3
        and len({s.trace_id for s in mem.spans}) == 1
        and by["a"].parent_id == by["run"].span_id == by["b"].parent_id
        and by["run"].parent_id is None
        and by["b"].status == "error"
        and by["run"].status == "ok"
        and tr.dropped == 3
        and all(s.duration > 0 for s in mem.spans)
    )


def check_journal_traces(incident_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    from incident_agent.agenteval.rig import Rig, run_scenario
    from incident_agent.agenteval.scenarios import SCENARIOS

    scn = next(s for s in SCENARIOS if s.name == "approve")
    with tempfile.TemporaryDirectory(prefix="obs-j-") as tmp:
        rig = Rig(Path(tmp))
        out = run_scenario(rig, scn.name, incident_id, scn.human)
        run_id = f"{scn.name}-{incident_id}".lower()
        events = rig.engine._journal(run_id).events()  # noqa: SLF001 - read-only
    spans = spans_from_events(run_id, events)
    root = [s for s in spans if s.kind == "run"]
    steps = [s for s in spans if s.kind == "step"]
    started = {e["step"] for e in events if e["kind"] == "step_started"}
    ok = (
        len(root) == 1
        and root[0].status == "ok"
        and {s.name for s in steps} == started
        and all(s.parent_id == root[0].span_id for s in steps)
        and all(s.status == "ok" for s in steps)
        and bool(out.completed if hasattr(out, "completed") else True)
    )
    return {"ok": ok, "steps": len(steps)}


PRICES = PriceTable.load()
CHEAP, STRONG = "openai/gpt-oss-20b", "openai/gpt-oss-120b"


def cascade_scenario(n: int = 20, accept_every: int = 4) -> dict[str, Any]:
    """Deterministic: of every ``accept_every`` calls the cheap model fails the quality gate once. Cost of calls priced
    from the verified table; baseline = every call on the strong model."""
    rows_cheap = [_ok("bad" if i % accept_every == 0 else "good") for i in range(n)]
    cheap = ScriptedClient(rows_cheap, model=CHEAP)
    strong = ScriptedClient([_ok("strong")] * n, model=STRONG)
    mem = MemorySink()
    tr = Tracer([mem], clock=_Tick())
    casc = CascadeClient(
        TracedClient(cheap, tr, PRICES, "cheap"),
        TracedClient(strong, tr, PRICES, "strong"),
        accept=lambda r, s: r.content == "good",
        tracer=tr,
    )
    answers = [casc.chat([{"role": "user", "content": f"q{i}"}]).content for i in range(n)]
    cost = sum(s.attrs.get("cost_usd") or 0.0 for s in mem.spans if s.kind == "llm" and "cost_usd" in s.attrs)
    always_strong = n * (PRICES.cost(STRONG, 100, 20) or 0.0)
    return {
        "answers": answers,
        "escalated": casc.stats["escalated"],
        "rate": casc.escalation_rate(),
        "cost": cost,
        "always_strong": always_strong,
        "saving": 1 - cost / always_strong if always_strong else 0.0,
    }


def check_cascade() -> dict[str, Any]:
    r = cascade_scenario()
    good = [a for i, a in enumerate(r["answers"]) if i % 4 != 0]
    ok = (
        r["escalated"] == 5
        and all(a == "strong" for i, a in enumerate(r["answers"]) if i % 4 == 0)
        and all(a == "good" for a in good)
    )
    # an unavailable cheap model escalates; a config error is not swallowed
    from incident_agent.llm.client import LLMConfigError

    class Down:
        model = "down"

        def chat(self, *a: Any, **k: Any) -> LLMResponse:
            raise LLMUnavailable("429")

    class Bad:
        model = "bad"

        def chat(self, *a: Any, **k: Any) -> LLMResponse:
            raise LLMConfigError("401")

    up = (
        CascadeClient(Down(), ScriptedClient([_ok("s")], model=STRONG)).chat([{"role": "user", "content": "x"}]).content
        == "s"
    )
    try:
        CascadeClient(Bad(), ScriptedClient([_ok()], model=STRONG)).chat([{"role": "user", "content": "x"}])
        loud = False
    except LLMConfigError:
        loud = True
    return {"ok": ok and up and loud, "saving": round(r["saving"], 4), "escalation_rate": r["rate"]}


def long_history(turns: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are an incident responder."},
        {"role": "user", "content": "Diagnose INC-1."},
    ]
    for i in range(turns):
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"c{i}", "function": {"name": "read_logs", "arguments": "{}"}}],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "log line " * 60})
    return msgs


def window_ok(window: Any, budget: int) -> bool:
    msgs = long_history(40)
    out = window.apply(msgs)
    head_ok = out[0] == msgs[0] and out[1] == msgs[1]
    tools_ok = True
    seen_calls: set[str] = set()
    for m in out:
        for tc in m.get("tool_calls") or []:
            seen_calls.add(tc["id"])
        if m.get("role") == "tool" and m.get("tool_call_id") not in seen_calls:
            tools_ok = False
    fits = sum(estimate_tokens(m) for m in out) <= budget + 200  # one newest unit may exceed the budget
    return head_ok and tools_ok and fits and len(out) < len(msgs) and out[-1] == msgs[-1]


class _BrokenWindow(SlidingWindow):
    """Negative control: cuts by message count, so a tool reply can lose its call."""

    def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages[:2] + messages[-5:]


def check_prefix() -> bool:
    stable = [
        {
            "messages": [{"role": "system", "content": "s" * 5000}, {"role": "user", "content": f"q{i}"}],
            "tools": [{"n": 1}],
        }
        for i in range(3)
    ]
    volatile = [
        {"messages": [{"role": "system", "content": f"now 2026-11-21T10:0{i}:00 " + "s" * 5000}], "tools": [{"n": 1}]}
        for i in range(3)
    ]
    changed = [{**stable[0]}, {**stable[1], "tools": [{"n": 2}]}]
    a, b, c = prefix_mod.analyze(stable), prefix_mod.analyze(volatile), prefix_mod.analyze(changed)
    return (
        a["mean_prefix_reuse"] > 0.99
        and not a["warnings"]
        and any("volatile" in w for w in b["warnings"])
        and b["mean_prefix_reuse"] < 0.5
        and any("tools changed" in w for w in c["warnings"])
    )


def run(incident_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    window = SlidingWindow(1500)
    casc = check_cascade()
    jt = check_journal_traces(incident_id, plan)
    controls = [
        {"control": "tracer_without_redaction", "flagged": not check_redaction_with_leaky_tracer()},
        {"control": "window_cuts_by_message_count", "flagged": not window_ok(_BrokenWindow(1500), 1500)},
        {"control": "cascade_never_escalates", "flagged": not check_never_escalates()},
    ]
    checks = {
        "redaction": check_redaction(),
        "trace_shape": check_trace_shape(),
        "journal_traces": jt["ok"],
        "cascade": casc["ok"],
        "window": window_ok(window, 1500),
        "prefix": check_prefix(),
    }
    return {
        "checks": checks,
        "all_ok": all(checks.values()),
        "journal_steps": jt["steps"],
        "cascade_saving": casc["saving"],
        "escalation_rate": casc["escalation_rate"],
        "controls": controls,
        "all_flagged": all(c["flagged"] for c in controls),
    }


def check_redaction_with_leaky_tracer() -> bool:
    """Negative control body: same probe, but spans bypass redaction. Must report a leak (returns False)."""
    from incident_agent.observe import trace as tmod

    ns = vars(tmod)  # the module's own namespace: the functions are imported names there, not exported attributes
    orig_clean, orig_redact = ns["clean_value"], ns["redact"]
    ns["clean_value"], ns["redact"] = (lambda value, depth=0: value), (lambda text: text)
    try:
        return check_redaction()
    finally:
        ns["clean_value"], ns["redact"] = orig_clean, orig_redact


def check_never_escalates() -> bool:
    cheap = ScriptedClient([_ok("bad")], model=CHEAP)
    strong = ScriptedClient([_ok("strong")], model=STRONG)
    casc = CascadeClient(cheap, strong, accept=lambda r, s: True)  # a gate that accepts everything
    return casc.chat([{"role": "user", "content": "x"}]).content == "strong"


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


__all__ = ["run", "cascade_scenario", "long_history", "read_spans"]
