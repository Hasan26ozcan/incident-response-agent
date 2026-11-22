# ruff: noqa: E501, UP031
"""Cost / latency report + dashboard (Stage 21), computed from spans only (no network)."""

from __future__ import annotations

import html
import json
import statistics
from typing import Any

from incident_agent.observe.pricing import PriceTable
from incident_agent.observe.trace import Span


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(spans: list[Span], prices: PriceTable | None = None) -> dict[str, Any]:
    prices = prices or PriceTable.load()
    llm = [s for s in spans if s.kind == "llm" and "prompt_tokens" in s.attrs]
    rows = [
        {
            "model": s.attrs.get("model", "?"),
            "prompt_tokens": s.attrs.get("prompt_tokens", 0),
            "completion_tokens": s.attrs.get("completion_tokens", 0),
            "cached_tokens": s.attrs.get("cached_tokens", 0),
        }
        for s in llm
    ]
    cost = prices.summarize(rows)
    lat = [s.duration for s in llm]
    pt = sum(r["prompt_tokens"] for r in rows)
    cached = sum(r["cached_tokens"] for r in rows)
    runs = [s for s in spans if s.kind == "run"]
    steps = [s for s in spans if s.kind == "step"]
    by_step: dict[str, list[float]] = {}
    for s in steps:
        by_step.setdefault(s.name, []).append(s.duration)
    route = [s for s in spans if s.name == "cascade.route"]
    esc = sum(1 for s in route if s.attrs.get("tier") == "strong")
    return {
        "spans": len(spans),
        "traces": len({s.trace_id for s in spans}),
        "errors": sum(1 for s in spans if s.status == "error"),
        "llm": {
            "calls": len(llm),
            "prompt_tokens": pt,
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "cached_tokens": cached,
            "cache_hit_ratio": round(cached / pt, 4) if pt else 0.0,
            "latency_p50": round(pct(lat, 0.5), 4),
            "latency_p95": round(pct(lat, 0.95), 4),
            "latency_mean": round(statistics.fmean(lat), 4) if lat else 0.0,
        },
        "cost": cost,
        "cascade": {
            "decisions": len(route),
            "escalated": esc,
            "escalation_rate": round(esc / len(route), 4) if route else 0.0,
        },
        "runs": {
            "count": len(runs),
            "ok": sum(1 for s in runs if s.status == "ok"),
            "error": sum(1 for s in runs if s.status == "error"),
            "duration_p50": round(pct([s.duration for s in runs], 0.5), 4),
            "duration_p95": round(pct([s.duration for s in runs], 0.95), 4),
        },
        "steps": {
            n: {"count": len(v), "p50": round(pct(v, 0.5), 4), "p95": round(pct(v, 0.95), 4)}
            for n, v in sorted(by_step.items())
        },
    }


def render_markdown(s: dict[str, Any]) -> str:
    c, ll = s["cost"], s["llm"]
    lines = [
        "# Cost & latency report",
        "",
        f"- spans: {s['spans']} in {s['traces']} trace(s); errors: {s['errors']}",
        f"- LLM calls: {ll['calls']}; tokens in/out/cached: {ll['prompt_tokens']}/{ll['completion_tokens']}/{ll['cached_tokens']} (cache hit {ll['cache_hit_ratio']:.1%})",
        f"- LLM latency p50/p95/mean: {ll['latency_p50']}s / {ll['latency_p95']}s / {ll['latency_mean']}s",
        f"- cost (priced models only): ${c['total_usd']:.6f}; unpriced calls: {c['unpriced_calls']} (unknown, not zero)",
        f"- cascade: {s['cascade']['escalated']}/{s['cascade']['decisions']} escalated ({s['cascade']['escalation_rate']:.1%})",
        f"- runs: {s['runs']['count']} (ok {s['runs']['ok']}, error {s['runs']['error']}); duration p50/p95 {s['runs']['duration_p50']}s / {s['runs']['duration_p95']}s",
        "",
        "| model | calls | in | out | cached | cost USD |",
        "|---|---|---|---|---|---|",
    ]
    for m, e in c["by_model"].items():
        lines.append(
            f"| {m} | {e['calls']} | {e['prompt_tokens']} | {e['completion_tokens']} | {e['cached_tokens']} | {('%.6f' % e['cost_usd']) if e['priced'] else 'unpriced'} |"
        )
    lines += ["", "| step | n | p50 s | p95 s |", "|---|---|---|---|"]
    lines += [f"| {n} | {e['count']} | {e['p50']} | {e['p95']} |" for n, e in s["steps"].items()]
    return "\n".join(lines) + "\n"


def render_dashboard(s: dict[str, Any], title: str = "Incident agent - observability") -> str:
    """Self-contained HTML (no scripts, no external assets); every value is escaped."""
    e = html.escape
    tiles = [
        ("LLM calls", s["llm"]["calls"]),
        ("Cost (USD)", f"{s['cost']['total_usd']:.6f}"),
        ("Cache hit", f"{s['llm']['cache_hit_ratio']:.1%}"),
        ("LLM p95", f"{s['llm']['latency_p95']}s"),
        ("Escalation", f"{s['cascade']['escalation_rate']:.1%}"),
        ("Run errors", s["runs"]["error"]),
    ]
    t = "".join(f"<div class=t><b>{e(str(v))}</b><span>{e(k)}</span></div>" for k, v in tiles)
    mx = max([v["p95"] for v in s["steps"].values()] + [1e-9])
    bars = "".join(
        f"<tr><td>{e(n)}</td><td><i style='width:{max(v['p95'] / mx * 100, 1):.1f}%'></i></td><td>{v['p95']}s</td></tr>"
        for n, v in s["steps"].items()
    )
    models = "".join(
        f"<tr><td>{e(m)}</td><td>{x['calls']}</td><td>{x['prompt_tokens']}</td><td>{x['completion_tokens']}</td><td>{x['cached_tokens']}</td><td>{('%.6f' % x['cost_usd']) if x['priced'] else 'unpriced'}</td></tr>"
        for m, x in s["cost"]["by_model"].items()
    )
    return (
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{e(title)}</title>"
        "<style>:root{--bg:#fff;--fg:#1a1a1a;--m:#666;--c:#2563eb}@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--m:#999;--c:#60a5fa}}"
        "body{background:var(--bg);color:var(--fg);font:15px system-ui;margin:0;padding:16px;max-width:900px}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}"
        ".t{border:1px solid #8884;border-radius:8px;padding:10px}.t b{display:block;font-size:22px}.t span{color:var(--m);font-size:12px}table{border-collapse:collapse;width:100%;margin:12px 0}"
        "td,th{padding:4px 8px;text-align:left;border-bottom:1px solid #8883}td i{display:block;height:10px;background:var(--c);border-radius:3px}</style>"
        f"<h1>{e(title)}</h1><div class=g>{t}</div><h2>Step latency (p95)</h2><table>{bars}</table>"
        f"<h2>Models</h2><table><tr><th>model<th>calls<th>in<th>out<th>cached<th>USD</tr>{models}</table>"
        f"<p style='color:var(--m)'>Unpriced models are shown as unpriced, never as zero. Generated from {s['spans']} spans.</p>"
    )


def to_json(s: dict[str, Any]) -> str:
    return json.dumps(s, indent=2, sort_keys=True)
