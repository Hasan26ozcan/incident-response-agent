# ruff: noqa: E501
"""Compare measured configurations: quality with bootstrap confidence intervals, paired wins/losses against a reference,
priced cost per incident, latency, and (for cascade runs) the real saving versus always-strong.

Small samples are stated plainly: with 14 dev incidents a difference of a few points is noise, and the report says so."""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path
from typing import Any

N_BOOT = 2000
METRICS = ("root_cause_f1", "evidence_recall", "risk_match", "false_alarm_ok", "answered")


def load(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def bootstrap_ci(values: list[float], seed: int = 7) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)  # noqa: S311  # seeded bootstrap, not security
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(N_BOOT))
    return (round(means[int(0.025 * N_BOOT)], 3), round(means[int(0.975 * N_BOOT) - 1], 3))


def rows_of(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["incident_id"]: r for r in run["incidents"]}


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    inc = [r for r in run["incidents"] if not r["error"]]
    ex = run.get("measure", {})
    out: dict[str, Any] = {
        "n": len(run["incidents"]),
        "crashed": sum(bool(r["error"]) for r in run["incidents"]),
        "complete": run["meta"].get("complete", False),
    }
    for m in METRICS:
        vals = [float(r[m]) for r in inc]
        out[m] = round(statistics.fmean(vals), 3) if vals else 0.0
        out[m + "_ci"] = bootstrap_ci(vals)
    cost = [ex[i]["cost_usd"] for i in ex if i in rows_of(run)]
    lat = [ex[i]["latency_s"] for i in ex if i in rows_of(run)]
    out["cost_usd_total"] = round(sum(cost), 6)
    out["cost_usd_per_incident"] = round(sum(cost) / len(cost), 6) if cost else 0.0
    out["unpriced_calls"] = sum(ex[i]["unpriced_calls"] for i in ex)
    out["llm_calls"] = sum(ex[i]["calls"] for i in ex)
    out["latency_s_mean"] = round(statistics.fmean(lat), 2) if lat else 0.0
    out["latency_s_max"] = round(max(lat), 2) if lat else 0.0
    return out


def paired(ref: dict[str, Any], other: dict[str, Any], metric: str = "root_cause_f1") -> dict[str, Any]:
    a, b = rows_of(ref), rows_of(other)
    common = sorted(set(a) & set(b))
    diffs = [float(b[i][metric]) - float(a[i][metric]) for i in common if not a[i]["error"] and not b[i]["error"]]
    wins, losses = sum(d > 0.02 for d in diffs), sum(d < -0.02 for d in diffs)
    return {
        "n": len(diffs),
        "mean_diff": round(statistics.fmean(diffs), 3) if diffs else 0.0,
        "ci": bootstrap_ci(diffs),
        "wins": wins,
        "losses": losses,
        "ties": len(diffs) - wins - losses,
    }


def cascade_view(run: dict[str, Any], strong_ref: dict[str, Any] | None) -> dict[str, Any] | None:
    ex = run.get("measure", {})
    models = {m for e in ex.values() for m in e["by_model"]}
    if len(models) < 2 or not strong_ref:
        return None
    strong_model = strong_ref["meta"]["model"].split("/", 1)[1]
    calls_strong = sum(e["by_model"].get(strong_model, {}).get("calls", 0) for e in ex.values())
    calls_all = sum(e["calls"] for e in ex.values())
    ref_cost = sum(e["cost_usd"] for e in strong_ref.get("measure", {}).values())
    my_cost = sum(e["cost_usd"] for e in ex.values())
    return {
        "strong_call_share": round(calls_strong / calls_all, 3) if calls_all else 0.0,
        "cost_usd": round(my_cost, 6),
        "always_strong_cost_usd": round(ref_cost, 6),
        "saving": round(1 - my_cost / ref_cost, 3) if ref_cost else None,
    }


def build(campaign_dir: Path, reference: str | None = None) -> dict[str, Any]:
    runs: dict[str, dict[str, dict[str, Any]]] = {}
    for p in sorted(campaign_dir.glob("*__*.json")):
        run = load(p)
        runs.setdefault(run["meta"]["set"], {})[run["meta"]["config"]] = run
    report: dict[str, Any] = {"sets": {}}
    for set_name, by_cfg in runs.items():
        ref_name = reference if reference in by_cfg else next(iter(by_cfg))
        ref = by_cfg[ref_name]
        sec: dict[str, Any] = {"reference": ref_name, "configs": {}}
        for name, run in by_cfg.items():
            entry = {"model": run["meta"]["model"], "agent": run["meta"]["agent"], "summary": summarize_run(run)}
            if name != ref_name:
                entry["vs_reference"] = paired(ref, run)
            cv = cascade_view(run, by_cfg.get("react-120b") or ref)
            if cv:
                entry["cascade"] = cv
            sec["configs"][name] = entry
        report["sets"][set_name] = sec
    return report


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [
        "# Measured results (real providers)",
        "",
        "Quality = token-overlap F1 against gold root cause (a crude metric; see docs). 95% intervals are bootstrap over incidents.",
        "",
    ]
    for set_name, sec in rep["sets"].items():
        lines += [
            f"## {set_name} set (reference: {sec['reference']})",
            "",
            "| config | model | n | crashed | F1 [95% CI] | evid. recall | risk acc | false-alarm acc | $/incident | latency s (mean/max) | vs ref (F1 diff, W/T/L) |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for name, e in sec["configs"].items():
            s = e["summary"]
            v = e.get("vs_reference")
            vs = (
                f"{v['mean_diff']:+.3f} [{v['ci'][0]:+.3f},{v['ci'][1]:+.3f}], {v['wins']}/{v['ties']}/{v['losses']}"
                if v
                else "-"
            )
            lines.append(
                f"| {name} | {e['model']} | {s['n']} | {s['crashed']} | {s['root_cause_f1']:.3f} [{s['root_cause_f1_ci'][0]:.2f},{s['root_cause_f1_ci'][1]:.2f}] | "
                f"{s['evidence_recall']:.3f} | {s['risk_match']:.3f} | {s['false_alarm_ok']:.3f} | {s['cost_usd_per_incident']:.5f}"
                f"{' (+' + str(s['unpriced_calls']) + ' unpriced calls)' if s['unpriced_calls'] else ''} | {s['latency_s_mean']}/{s['latency_s_max']} | {vs} |"
            )
        for name, e in sec["configs"].items():
            if "cascade" in e:
                c = e["cascade"]
                saving = "n/a" if c["saving"] is None else f"{c['saving']:.1%}"
                lines += [
                    "",
                    f"**{name} (cascade):** {c['strong_call_share']:.1%} of calls reached the strong model; cost ${c['cost_usd']:.6f} vs always-strong ${c['always_strong_cost_usd']:.6f} (saving {saving}).",
                    "A saving only counts if quality is not lower: check the F1 row and the W/T/L column above.",
                ]
        lines.append("")
        n = max(e["summary"]["n"] for e in sec["configs"].values())
        if n < 30:
            lines += [
                f"> Sample size {n}: differences of a few points are within noise. Treat rankings as indicative, not proven.",
                "",
            ]
    lines += [
        "Costs use the verified price table; models without a verified price are counted as 'unpriced', never as free."
    ]
    return "\n".join(lines) + "\n"
