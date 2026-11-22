"""``python -m incident_agent.observe`` - report | demo | prices | wandb.  Local, offline; exporters are opt-in."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.observe import report as rep
from incident_agent.observe.journal_traces import spans_from_root
from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.trace import read_spans


def _write(out: Path, spans: list, name: str = "observability") -> None:  # type: ignore[type-arg]
    s = rep.summarize(spans)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.md").write_text(rep.render_markdown(s), encoding="utf-8")
    (out / f"{name}.html").write_text(rep.render_dashboard(s), encoding="utf-8")
    (out / f"{name}.json").write_text(rep.to_json(s), encoding="utf-8")
    print(rep.render_markdown(s))
    print(f"wrote {out / (name + '.html')} (+ .md, .json)")


def _demo(out: Path) -> int:
    """A simulated, offline run: scripted LLMs on a tick clock. Shows the report; costs nothing."""
    from incident_agent.observe import selfcheck as sc
    from incident_agent.observe.trace import JsonlSink, Tracer

    out.mkdir(parents=True, exist_ok=True)
    trace_file = out / "demo-trace.jsonl"
    trace_file.unlink(missing_ok=True)
    tr = Tracer([JsonlSink(trace_file)], clock=sc._Tick())  # noqa: SLF001
    prices = PriceTable.load()
    from incident_agent.llm.scripted import ScriptedClient
    from incident_agent.observe.cascade import CascadeClient
    from incident_agent.observe.llm import TracedClient

    cheap = ScriptedClient(
        [sc._ok("bad" if i % 4 == 0 else "good", 900, 80, 512 if i % 4 else 0) for i in range(12)], model=sc.CHEAP
    )  # noqa: SLF001
    strong = ScriptedClient([sc._ok("strong", 900, 120) for _ in range(12)], model=sc.STRONG)  # noqa: SLF001
    casc = CascadeClient(
        TracedClient(cheap, tr, prices, "cheap"),
        TracedClient(strong, tr, prices, "strong"),
        lambda r, s: r.content == "good",
        tr,
    )
    with tr.span("run:demo", "run"):
        for i in range(12):
            with tr.span("diagnose", "step"):
                casc.chat([{"role": "user", "content": f"q{i}"}])
    print("SIMULATED: scripted models and a tick clock; numbers show the mechanics, not real provider behaviour.\n")
    _write(out, read_spans(trace_file), "demo")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incident_agent.observe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="cost/latency report + dashboard from a trace file and/or durable run journals")
    r.add_argument("--trace", type=Path, help="JSONL trace file")
    r.add_argument("--runs", type=Path, help="durable runs directory (journals become traces)")
    r.add_argument("--out", type=Path, default=Path("observability"))
    d = sub.add_parser("demo", help="simulated offline demo (no API calls)")
    d.add_argument("--out", type=Path, default=Path("observability"))
    sub.add_parser("prices", help="show the verified price table")
    w = sub.add_parser("wandb", help="log the agentic gate metrics as a W&B experiment (offline by default)")
    w.add_argument("--name", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "prices":
        for m, p in sorted(PriceTable.load().models.items()):
            print(f"{m}: in ${p.input}/1M, out ${p.output}/1M, cached in {p.cached_input}")
        return 0
    if a.cmd == "demo":
        return _demo(a.out)
    if a.cmd == "wandb":
        from incident_agent.agenteval import suite
        from incident_agent.observe.exporters import ExporterUnavailable, log_wandb

        o = suite.run_suite(("observability",))["observability"]
        try:
            print(
                "logged to",
                log_wandb(
                    {
                        "checks_ok": o["all_ok"],
                        "cascade_saving": o["cascade_saving"],
                        "escalation_rate": o["escalation_rate"],
                    },
                    name=a.name,
                ),
            )
        except ExporterUnavailable as exc:
            print(f"W&B unavailable: {exc}", file=sys.stderr)
            return 2
        return 0
    spans = []
    if a.trace:
        spans += read_spans(a.trace)
    if a.runs:
        spans += spans_from_root(a.runs)
    if not spans:
        print("no spans found (give --trace and/or --runs)", file=sys.stderr)
        return 2
    _write(a.out, spans)
    return 0


if __name__ == "__main__":
    sys.exit(main())
