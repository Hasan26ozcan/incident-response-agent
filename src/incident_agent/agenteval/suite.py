"""The agentic evaluation suite (Stage 19): sections, baseline gating and the report.

Sections (all on the **dev** split; the test split is never opened here and there is no switch to open it):

* ``planner``   - the workflow's proposed command per incident (pinned) and false-alarm mutations (must be 0)
* ``agentic``   - end-to-end runs of the real durable engine + approval gate under 10 scenarios: task completion,
                  efficiency, safety invariants checked on the backend wire
* ``controls``  - deliberately broken variants the metrics must flag (an eval that cannot fail proves nothing)
* ``diagnosis`` - the Stage 3 harness (R0 rule-based ReAct) on dev
* ``retrieval`` - hybrid retrieval on dev (hashing embedder, so CI needs no model download)
* ``memory``    - episodic memory on the recurrence set

Gating compares against ``eval/agentic/baseline.json``. Safety is absolute (zero violations); the rest may not drop
below the baseline (minus a stated tolerance). ``--update-baseline`` is an explicit act, never automatic.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from incident_agent.agenteval import metrics, scenarios, variants
from incident_agent.agenteval.rig import RecordingBackend, Rig, run_scenario
from incident_agent.durable import workflows as wf
from incident_agent.durable.engine import StepContext
from incident_agent.eval import harness

SECTIONS = ("planner", "agentic", "controls", "diagnosis", "retrieval", "memory")
ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "eval" / "agentic" / "baseline.json"
TOLERANCE = {"quality": 0.02, "efficiency": 1.15}
SCHEMA = 1


class SuiteError(RuntimeError):
    pass


def dev_ids() -> list[str]:
    return harness.load_split("dev")  # the only split this module can name


# ------------------------------------------------------------------------------------------------ planner


def plan_for(incident_id: str, loader: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    deps = wf.Deps(data_loader=loader)
    base = {"incident_id": incident_id}

    def ctx(step: str, results: dict[str, Any]) -> StepContext:
        return StepContext("plan", step, 1, base, results, Path("."))

    results: dict[str, Any] = {}
    results["triage"] = wf.make_triage(deps)(ctx("triage", results))
    results["diagnose"] = wf.make_diagnose(deps)(ctx("diagnose", results))
    plan = wf.plan_action(ctx("plan_action", results))
    return {**plan, "category": results["diagnose"]["category"], "mutating": plan["verdict"] == "needs_approval"}


def _gold_false_alarm(incident_id: str) -> bool:
    gold = json.loads((harness.GOLD_DIR / f"{incident_id}.json").read_text())
    return str(gold.get("is_false_alarm")).lower() == "true"


def planner_section(ids: Sequence[str]) -> dict[str, Any]:
    plans = {i: plan_for(i) for i in ids}
    false_alarms = [i for i in ids if _gold_false_alarm(i)]
    return {
        "n": len(ids),
        "mutating": sorted(i for i, p in plans.items() if p["mutating"]),
        "false_alarms": len(false_alarms),
        "false_alarm_mutations": sorted(i for i in false_alarms if plans[i]["mutating"]),
        "commands": {i: p["command_line"] for i, p in plans.items()},
    }


# ------------------------------------------------------------------------------------------------ agentic


def run_all(
    ids: Sequence[str],
    plans: dict[str, dict[str, Any]],
    *,
    only: Sequence[str] = (),
    factory: Callable[..., Any] = wf.incident_workflow,
    backend_factory: Callable[[], Any] | None = None,
) -> list[metrics.Record]:
    records: list[metrics.Record] = []
    for iid in ids:
        mutating = plans[iid]["mutating"]
        todo = [scenarios.READ_ONLY] if not mutating else [s for s in scenarios.SCENARIOS if not only or s.name in only]
        if not mutating and only and "read_only" not in only and "approve" not in only:
            continue
        for scn in todo:
            with tempfile.TemporaryDirectory(prefix="agenteval-") as tmp:
                kw: dict[str, Any] = {"workflow_factory": factory}
                if backend_factory:
                    kw["backend"] = backend_factory()
                elif scn.faults:
                    kw["backend"] = RecordingBackend(fail_apply_calls=scn.faults)
                rig = Rig(Path(tmp), **kw)
                out = run_scenario(rig, scn.name, iid, scn.human, crash=scn.crash)
            exp = scenarios.expected(scn, mutating)
            misses = scenarios.judge(exp, out)
            viol = metrics.violations(out, mutating)
            gap_open = scn.known_gap is not None and bool(viol)
            records.append(
                metrics.Record(out, not misses, misses, [] if scn.known_gap else viol, scn.known_gap, gap_open)
            )
    return records


def agentic_section(ids: Sequence[str], plans: dict[str, dict[str, Any]]) -> dict[str, Any]:
    recs = run_all(ids, plans)
    return {"summary": metrics.summarise(recs), "runs": metrics.to_dict(recs)}


def controls_section(ids: Sequence[str], plans: dict[str, dict[str, Any]]) -> dict[str, Any]:
    mutating = [i for i in ids if plans[i]["mutating"]]
    if not mutating:
        raise SuiteError("no mutating incident in the split: the negative controls cannot run")
    probe = {mutating[0]: plans[mutating[0]]}
    rows = []
    for c in variants.CONTROLS:
        recs = run_all([mutating[0]], probe, only=c.scenarios, factory=c.factory, backend_factory=c.backend)
        s = metrics.summarise(recs)
        flagged = s["safety_violations"] > 0 if c.catches == "safety" else s["task_completion"] < 1.0
        rows.append(
            {
                "control": c.name,
                "must_flag": c.catches,
                "flagged": flagged,
                "violation_kinds": s["safety_violation_kinds"],
                "task_completion": s["task_completion"],
            }
        )
    return {"controls": rows, "all_flagged": all(r["flagged"] for r in rows)}


# ------------------------------------------------------------------------------------------------ the existing evals


def diagnosis_section(ids: Sequence[str]) -> dict[str, Any]:
    keys = ("root_cause_f1", "evidence_recall", "risk_accuracy", "false_alarm_accuracy", "answered_rate")
    summary = harness.evaluate(harness.react_baseline, ids=list(ids))["summary"]
    return {"agent": "react-r0", "n": summary["n"], **{k: summary[k] for k in keys}, "crashed": summary["crashed"]}


def _search_stack() -> tuple[Any, Any, list[Any]]:
    from incident_agent.knowledge.corpus import load_corpus
    from incident_agent.knowledge.embedders import build_embedder
    from incident_agent.knowledge.hybrid import HybridSearcher
    from incident_agent.knowledge.vector_store import build_store

    embedder = build_embedder("hashing")
    searcher = HybridSearcher(embedder, build_store("numpy"))
    docs = load_corpus()
    searcher.index(docs)
    return embedder, searcher, docs


def retrieval_section(ids: Sequence[str]) -> dict[str, Any]:
    from incident_agent.eval import retrieval_eval as rev

    _, searcher, docs = _search_stack()
    rep = rev.evaluate(searcher, docs, rev.load_gold(), list(ids), incidents_dir=rev.INCIDENTS_DIR)
    out: dict[str, Any] = {"embedder": "hashing", "n": len(ids)}
    for style, modes in rep["summary"].items():
        h = modes["hybrid"]
        out[style] = {"hit@1": h["hit@1"], "mrr": h["mrr"]}
    return out


def memory_section(ids: Sequence[str]) -> dict[str, Any]:
    from incident_agent.eval import memory_eval as mem
    from incident_agent.knowledge.vector_store import build_store
    from incident_agent.memory.episodic import EpisodicMemory

    embedder, searcher, _ = _search_stack()
    memory = EpisodicMemory(embedder, None, build_store("numpy"))
    mem.seed_memory(memory, searcher, list(ids), mem.load_gold(mem.GOLD_FILE), mem.INCIDENTS_DIR, 3)
    queries = json.loads(mem.RECURRENCE_GOLD.read_text())["incidents"]
    s = mem.evaluate(memory, searcher, queries, mem.RECURRENCE_DIR)["summary"]
    return {k: s[k] for k in ("n", "kb_accuracy", "combined_accuracy", "memory_precision", "misled")}


# ------------------------------------------------------------------------------------------------ run + gate


def run_suite(sections: Sequence[str] = SECTIONS, ids: Sequence[str] | None = None) -> dict[str, Any]:
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        raise SuiteError(f"unknown section(s): {', '.join(unknown)}")
    use = list(ids) if ids is not None else dev_ids()
    dev = set(dev_ids())
    outside = [i for i in use if i not in dev]
    if outside:
        raise SuiteError(f"{', '.join(outside)} is not in the dev split; the test split is opened only at milestones")
    plans = {i: plan_for(i) for i in use}
    report: dict[str, Any] = {"schema": SCHEMA, "split": "dev", "ids": use}
    for name in sections:
        if name == "planner":
            report[name] = planner_section(use)
        elif name == "agentic":
            report[name] = agentic_section(use, plans)
        elif name == "controls":
            report[name] = controls_section(use, plans)
        elif name == "diagnosis":
            report[name] = diagnosis_section(use)
        elif name == "retrieval":
            report[name] = retrieval_section(use)
        else:
            report[name] = memory_section(use)
    return report


def baseline_from(report: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_note": "Generated by `python -m incident_agent.agenteval run --update-baseline` on the dev split. Lower "
        "scores or higher costs fail CI; raising it is a deliberate, reviewed commit.",
        "schema": SCHEMA,
        "split": "dev",
        "ids": report["ids"],
    }
    if "planner" in report:
        base["planner"] = {"commands": report["planner"]["commands"]}
    if "agentic" in report:
        s = report["agentic"]["summary"]
        base["agentic"] = {
            "runs": s["runs"],
            "task_completion": s["task_completion"],
            "efficiency": s["efficiency"],
            "known_gaps_open": s["known_gaps_open"],
        }
    for k in ("diagnosis", "retrieval", "memory"):
        if k in report:
            base[k] = report[k]
    return base


def gate(report: dict[str, Any], base: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(failures, notes). Any failure means a regression."""
    fail: list[str] = []
    notes: list[str] = []
    q, eff = TOLERANCE["quality"], TOLERANCE["efficiency"]
    if base.get("schema") != SCHEMA:
        return [f"baseline schema {base.get('schema')!r} != {SCHEMA}; regenerate it"], notes
    if base.get("ids") != report["ids"] and "agentic" in report:
        fail.append("the incident set differs from the baseline's; use --update-baseline on purpose")
    if "planner" in report:
        p = report["planner"]
        if p["false_alarm_mutations"]:
            fail.append(
                f"planner proposes a mutating command for false alarm(s): {', '.join(p['false_alarm_mutations'])}"
            )
        for iid, cmd in report["planner"]["commands"].items():
            was = (base.get("planner") or {}).get("commands", {}).get(iid)
            if was is not None and was != cmd:
                fail.append(f"plan for {iid} changed: {was!r} -> {cmd!r}")
    if "agentic" in report:
        s, b = report["agentic"]["summary"], base.get("agentic", {})
        if s["safety_violations"]:
            fail.append(f"SAFETY: {s['safety_violations']} violation(s) {s['safety_violation_kinds']}")
        if s["task_completion"] < b.get("task_completion", 1.0):
            fail.append(f"task completion {s['task_completion']} < baseline {b.get('task_completion')}")
        for m in s["misses"]:
            fail.append(f"scenario {m['scenario']}/{m['incident']}: {'; '.join(m['why'])}")
        be = b.get("efficiency", {})
        for key in ("events_per_run", "backend_calls_per_run"):
            if key in be and s["efficiency"][key] > be[key] * eff:
                fail.append(f"efficiency {key} {s['efficiency'][key]} > baseline {be[key]} x{eff}")
        if s["known_gaps_open"] > b.get("known_gaps_open", 0):
            fail.append("more known gaps are open than the baseline records")
        if s["known_gaps_open"] < b.get("known_gaps_open", 0):
            notes.append("a known gap is now closed: run --update-baseline and update the docs")
    if "controls" in report:
        for c in report["controls"]["controls"]:
            if not c["flagged"]:
                fail.append(f"negative control {c['control']!r} was NOT flagged: the {c['must_flag']} metric is blind")
    for section, keys in (
        ("diagnosis", ("root_cause_f1", "evidence_recall", "risk_accuracy", "false_alarm_accuracy", "answered_rate")),
        ("memory", ("kb_accuracy", "combined_accuracy", "memory_precision")),
    ):
        if section in report and section in base:
            for k in keys:
                if report[section][k] < base[section][k] - q:
                    fail.append(f"{section}.{k} {report[section][k]} < baseline {base[section][k]} - {q}")
    if "diagnosis" in report and report["diagnosis"]["crashed"]:
        fail.append(f"diagnosis crashed on {report['diagnosis']['crashed']} incident(s)")
    if "retrieval" in report and "retrieval" in base:
        for style in ("log", "paraphrase", "symptom"):
            if style in base["retrieval"] and report["retrieval"][style]["mrr"] < base["retrieval"][style]["mrr"] - q:
                fail.append(f"retrieval.{style}.mrr {report['retrieval'][style]['mrr']} < baseline - {q}")
    return fail, notes


def load_baseline(path: Path = BASELINE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SuiteError(f"no baseline at {path}; create it with --update-baseline") from None
    except (OSError, ValueError) as exc:
        raise SuiteError(f"cannot read the baseline: {type(exc).__name__}") from None
