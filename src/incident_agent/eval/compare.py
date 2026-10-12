"""Compare result files: side-by-side summary, per-incident deltas, paired bootstrap confidence intervals.

With ~10 incidents per split a difference of a few points is noise. This tool says so: it reports the
paired bootstrap 95 % CI of the mean difference to the baseline and win/loss/tie counts, instead of just
two averages.

Usage:  python -m incident_agent.eval.compare llm_react__s6_single llm_orchestrator__s6_orch [--markdown]
        (first argument is the baseline; each name is a stem in eval/results/ or a path to a JSON file)
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incident_agent.eval.harness import RESULTS_DIR

METRICS = ("root_cause_f1", "evidence_recall", "risk_accuracy", "false_alarm_accuracy", "answered_rate")
_PER_INCIDENT = {
    "root_cause_f1": lambda r: r["root_cause_f1"],
    "evidence_recall": lambda r: r["evidence_recall"],
    "risk_accuracy": lambda r: float(r["risk_match"]),
    "false_alarm_accuracy": lambda r: float(r["false_alarm_ok"]),
    "answered_rate": lambda r: float(r["answered"]),
}


@dataclass
class Run:
    name: str
    report: dict[str, Any]
    judge: dict[str, float] | None = None  # incident_id -> judge score (0 / 0.5 / 1)

    @property
    def rows(self) -> dict[str, dict[str, Any]]:
        return {r["incident_id"]: r for r in self.report["incidents"]}


def load_run(name_or_path: str, results_dir: Path | None = None) -> Run:
    base = results_dir or RESULTS_DIR
    p = Path(name_or_path)
    path = p if p.suffix == ".json" and p.exists() else base / f"{name_or_path}.json"
    if not path.exists():
        raise FileNotFoundError(f"no result file for {name_or_path!r} (looked at {path})")
    report = json.loads(path.read_text())
    if not report.get("meta", {}).get("complete", True):
        print(f"WARNING: {path.name} is incomplete (run was cut off); numbers cover finished incidents only")
    judge_path = path.parent / f"judge__{path.stem.split('__', 1)[-1]}.json"
    judge = None
    if judge_path.exists():
        judge = {r["incident_id"]: r["score"] for r in json.loads(judge_path.read_text())["incidents"]}
    return Run(path.stem, report, judge)


def paired_bootstrap(deltas: list[float], n_resamples: int = 10000, seed: int = 0) -> tuple[float, float]:
    """95 % CI of the mean of paired differences (percentile bootstrap, fixed seed, no numpy needed)."""
    if not deltas:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(rng.choices(deltas, k=n)) / n for _ in range(n_resamples))
    return means[int(0.025 * n_resamples)], means[int(0.975 * n_resamples) - 1]


def compare_to_baseline(base: Run, other: Run, metric: str) -> dict[str, Any]:
    get = _PER_INCIDENT[metric]
    common = sorted(set(base.rows) & set(other.rows))
    deltas = [get(other.rows[i]) - get(base.rows[i]) for i in common]
    lo, hi = paired_bootstrap(deltas)
    return {
        "n": len(common),
        "mean_delta": (sum(deltas) / len(deltas)) if deltas else 0.0,
        "ci": (lo, hi),
        "wins": sum(d > 1e-9 for d in deltas),
        "losses": sum(d < -1e-9 for d in deltas),
        "ties": sum(abs(d) <= 1e-9 for d in deltas),
        "significant": bool(deltas) and (lo > 0 or hi < 0),
    }


def _cell(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def render(runs: list[Run], markdown: bool = False) -> str:
    out: list[str] = []
    cols = [*METRICS, "crashed", "degraded", "llm_calls", "prompt_tokens", "completion_tokens"]
    has_judge = any(r.judge for r in runs)
    header = ["run", "n", *cols, *(["judge"] if has_judge else [])]
    rows = []
    for r in runs:
        s = r.report["summary"]
        judge = (sum(r.judge.values()) / len(r.judge)) if r.judge else None
        rows.append([r.name, s["n"], *[s.get(c, 0) for c in cols], *([judge] if has_judge else [])])

    def line(cells: list[Any]) -> str:
        return (
            ("| " + " | ".join(_cell(c) for c in cells) + " |")
            if markdown
            else "  ".join(f"{_cell(c):<18}" for c in cells)
        )

    out.append(line(header))
    if markdown:
        out.append("|" + "---|" * len(header))
    out += [line(r) for r in rows]

    base = runs[0]
    for other in runs[1:]:
        out.append("")
        out.append(f"{other.name} vs baseline {base.name} (paired, per incident)")
        for m in METRICS[:4]:
            c = compare_to_baseline(base, other, m)
            tag = "SIGNIFICANT" if c["significant"] else "not significant"
            out.append(
                f"  {m:<22} delta {c['mean_delta']:+.3f}  95% CI [{c['ci'][0]:+.3f}, {c['ci'][1]:+.3f}]  "
                f"W/L/T {c['wins']}/{c['losses']}/{c['ties']}  n={c['n']}  {tag}"
            )
        calls_b, calls_o = base.report["summary"]["llm_calls"], other.report["summary"]["llm_calls"]
        if calls_b:
            out.append(f"  cost: {calls_o / calls_b:.1f}x the LLM calls of the baseline")
        out.append("  per-incident F1 delta: " + _per_incident_deltas(base, other))
    n = min(r.report["summary"]["n"] for r in runs)
    if n < 30:
        out.append("")
        out.append(f"NOTE: n={n}. With so few incidents, treat differences as indicative, not proven.")
    return "\n".join(out)


def _per_incident_deltas(base: Run, other: Run) -> str:
    parts = []
    for i in sorted(set(base.rows) & set(other.rows)):
        d = other.rows[i]["root_cause_f1"] - base.rows[i]["root_cause_f1"]
        parts.append(f"{i[-3:]}:{d:+.2f}")
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare eval result files (first one is the baseline)")
    ap.add_argument("runs", nargs="+", help="result stems in eval/results/ (e.g. llm_react__s6_single) or JSON paths")
    ap.add_argument("--markdown", action="store_true", help="print tables as Markdown (for docs/RESULTS.md)")
    args = ap.parse_args(argv)
    print(render([load_run(r) for r in args.runs], markdown=args.markdown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
