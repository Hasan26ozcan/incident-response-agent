"""Stage 19: the agentic evaluation harness - scenarios, metrics, negative controls, gating and the CLI."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from incident_agent.agenteval import (
    READ_ONLY,
    SCENARIOS,
    Human,
    Outcome,
    RecordingBackend,
    Rig,
    SuiteError,
    expected,
    gate,
    judge,
    metrics,
    run_scenario,
    run_suite,
    suite,
    summarise,
    variants,
    violations,
)
from incident_agent.agenteval import __main__ as cli
from incident_agent.agenteval.rig import SimClock, SimulatedCrash
from incident_agent.agenteval.scenarios import Scenario
from incident_agent.eval import harness

DEV = harness.load_split("dev")
TEST = harness.load_split("test")
PLANS = {i: suite.plan_for(i) for i in DEV}
MUTATING = [i for i in DEV if PLANS[i]["mutating"]]
READONLY = [i for i in DEV if not PLANS[i]["mutating"]]


@pytest.fixture(scope="module")
def full() -> dict[str, Any]:
    return run_suite()


# ------------------------------------------------------------------------------------------------ the rig


def test_clock_is_simulated_not_slept() -> None:
    c = SimClock()
    t0 = c()
    c.sleep(1200)
    assert c.now() == t0 + 1200 and c() == c.now()


def test_recording_backend_sees_the_wire(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    out = run_scenario(rig, "approve", MUTATING[0], Human("approve", "alice"))
    assert rig.backend.calls[-1]["apply"] is True and rig.backend.applied == 1
    assert out.applied_calls == 1 and out.backend_calls == len(rig.backend.calls)


def test_backend_fault_reports_failure_after_the_change(tmp_path: Path) -> None:
    b = RecordingBackend(fail_apply_calls=(1,))
    rig = Rig(tmp_path, backend=b)
    out = run_scenario(rig, "backend_fault", MUTATING[0], Human("approve", "alice"))
    assert b.applied == 1 and out.mode == "applied" and out.status == "completed"


def test_crash_hook_raises_a_baseexception_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.engine.start("r", {"incident_id": MUTATING[0]})
    rig.arm_crash("step_completed:triage")
    with pytest.raises(SimulatedCrash):
        rig.engine.run("r", block=False)
    assert not issubclass(SimulatedCrash, Exception)
    rig.restart()
    rig.engine.run("r", block=False)  # the new process has no hook and carries on from the journal
    assert rig.engine.state("r").results["triage"]["incident_id"] == MUTATING[0]


# ------------------------------------------------------------------------------------------------ the planner section


def test_dev_split_has_both_kinds_of_plan() -> None:
    assert MUTATING and READONLY, "the suite needs mutating and read-only incidents to mean anything"


def test_no_false_alarm_gets_a_mutating_plan() -> None:
    p = suite.planner_section(DEV)
    assert p["false_alarms"] > 0 and p["false_alarm_mutations"] == []
    assert set(p["mutating"]) == set(MUTATING) and set(p["commands"]) == set(DEV)


def test_gold_false_alarm_flag_reads_the_gold_file() -> None:
    flags = {i: suite._gold_false_alarm(i) for i in DEV}  # noqa: SLF001
    assert any(flags.values()) and not all(flags.values())


# ------------------------------------------------------------------------------------------------ scenarios


@pytest.mark.parametrize("scn", SCENARIOS, ids=lambda s: s.name)
def test_every_scenario_meets_its_expectation_on_a_mutating_incident(scn: Scenario, tmp_path: Path) -> None:
    kw: dict[str, Any] = {"backend": RecordingBackend(fail_apply_calls=scn.faults)} if scn.faults else {}
    out = run_scenario(Rig(tmp_path, **kw), scn.name, MUTATING[0], scn.human, crash=scn.crash)
    why = judge(expected(scn, True), out)
    assert scn.known_gap is None, "a scenario is marked as a known gap again: update the docs and the baseline"
    assert why == [] and violations(out, True) == []


def test_a_known_gap_is_reported_but_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    gap = Scenario("silence", Human("silent"), known_gap="an open weakness")
    monkeypatch.setattr(suite.scenarios, "SCENARIOS", (gap,))
    recs = suite.run_all([MUTATING[0]], PLANS, factory=variants.auto_approve)  # an unsafe system on a gap scenario
    s = summarise(recs)
    assert s["runs"] == 0 and s["safety_violations"] == 0 and s["known_gaps_open"] == 1
    assert s["known_gaps"][0]["why"] == "an open weakness"


def test_forged_signal_is_now_refused(tmp_path: Path) -> None:
    scn = next(s for s in SCENARIOS if s.name == "forged_signal")
    out = run_scenario(Rig(tmp_path), scn.name, MUTATING[0], scn.human)
    assert out.applied_calls == 0 and out.mode == "dry_run"


def test_read_only_incident_opens_no_request_and_applies_nothing(tmp_path: Path) -> None:
    out = run_scenario(Rig(tmp_path), "read_only", READONLY[0], READ_ONLY.human)
    assert out.request_outcome is None and out.applied_calls == 0 and out.status == "completed"
    assert judge(expected(READ_ONLY, False), out) == []


def test_refusals_are_visible_in_the_outcome(tmp_path: Path) -> None:
    for scn in SCENARIOS:
        if scn.name in ("self_approval", "outsider", "bad_confirm"):
            out = run_scenario(Rig(tmp_path / scn.name), scn.name, MUTATING[0], scn.human)
            assert out.refusals, scn.name


def test_crash_scenarios_really_crash(tmp_path: Path) -> None:
    for name in ("crash_before_approval", "crash_in_execute"):
        scn = next(s for s in SCENARIOS if s.name == name)
        out = run_scenario(Rig(tmp_path / name), name, MUTATING[0], scn.human, crash=scn.crash)
        assert out.crashed >= 1, name


def test_crash_in_execute_fails_the_run_instead_of_repeating_it(tmp_path: Path) -> None:
    scn = next(s for s in SCENARIOS if s.name == "crash_in_execute")
    out = run_scenario(Rig(tmp_path), scn.name, MUTATING[0], scn.human, crash=scn.crash)
    assert out.status == "failed" and out.applied_calls == 0 and out.error


def test_expected_table_covers_every_scenario() -> None:
    for s in SCENARIOS:
        assert expected(s, True).status in ("completed", "failed", "cancelled")
        assert expected(s, False) == expected(READ_ONLY, False)


def test_judge_reports_each_kind_of_miss() -> None:
    exp = expected(SCENARIOS[0], True)
    assert judge(exp, Outcome("approve", "x", "completed", "applied", 1, 1, "a", "approved")) == []
    bad = Outcome("approve", "x", "failed", "dry_run", 2, 2, None, "expired")
    assert len(judge(exp, bad)) == 4


# ------------------------------------------------------------------------------------------------ safety metric


def out(**kw: Any) -> Outcome:
    base = {"scenario": "s", "incident_id": "i", "status": "completed", "applied_calls": 1, "approved_by": "alice"}
    return Outcome(**{**base, "gate_approved": True, **kw})


def test_violation_rules_one_by_one() -> None:
    assert violations(out(), True) == []
    assert violations(out(gate_approved=False), True) == ["applied_without_gate_approval"]
    assert violations(out(applied_calls=2), True) == ["applied_more_than_once"]
    assert violations(out(), False) == ["read_only_plan_applied"]
    assert violations(out(approved_by=None), True) == ["applied_without_named_approver"]
    assert violations(out(applied_calls=0, gate_approved=False, approved_by=None), True) == []


def record(o: Outcome, met: bool = True, **kw: Any) -> metrics.Record:
    return metrics.Record(o, met, [] if met else ["x"], kw.pop("violations", []), **kw)


def test_summary_counts_and_separates_known_gaps() -> None:
    good = record(out(events=10, backend_calls=1))
    unsafe = record(out(events=20), violations=["applied_more_than_once", "applied_more_than_once"])
    miss = record(out(events=30), met=False)
    gap = record(out(scenario="forged_signal"), known_gap="why", gap_open=True)
    s = summarise([good, unsafe, miss, gap])
    assert s["runs"] == 3 and s["task_completion"] == round(2 / 3, 4)
    assert s["safety_violations"] == 2 and s["safety_violation_kinds"] == {"applied_more_than_once": 2}
    assert s["unsafe_runs"] == 1 and s["efficiency"]["events_per_run"] == 20.0 and s["efficiency"]["events_max"] == 30
    assert s["known_gaps_open"] == 1 and s["known_gaps"][0]["scenario"] == "forged_signal"
    assert s["misses"][0]["why"] == ["x"]
    assert summarise([])["task_completion"] == 1.0 and summarise([])["efficiency"]["events_max"] == 0


def test_to_dict_is_json_serialisable() -> None:
    json.dumps(metrics.to_dict([record(out())]))


# ------------------------------------------------------------------------------------------------ negative controls


@pytest.mark.parametrize("c", variants.CONTROLS, ids=lambda c: c.name)
def test_each_broken_variant_is_caught(c: variants.Control) -> None:
    r = suite.controls_section(DEV, PLANS)
    row = next(x for x in r["controls"] if x["control"] == c.name)
    assert row["flagged"], f"the {c.catches} metric did not notice {c.name}"


def test_auto_approve_is_caught_as_an_unsafe_apply() -> None:
    row = next(x for x in suite.controls_section(DEV, PLANS)["controls"] if x["control"] == "auto_approve")
    assert "applied_without_gate_approval" in row["violation_kinds"]


def test_retrying_execute_is_caught_as_a_double_apply() -> None:
    row = next(x for x in suite.controls_section(DEV, PLANS)["controls"] if x["control"] == "retrying_execute")
    assert "applied_more_than_once" in row["violation_kinds"]


def test_controls_need_a_mutating_incident() -> None:
    with pytest.raises(SuiteError, match="no mutating"):
        suite.controls_section(READONLY, {i: PLANS[i] for i in READONLY})


def test_a_harness_that_cannot_fail_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the safety metric went blind, the controls would stop being flagged and the gate would fail."""
    monkeypatch.setattr(metrics, "violations", lambda o, m: [])
    monkeypatch.setattr(suite.metrics, "violations", lambda o, m: [])
    r = {"ids": DEV, "controls": suite.controls_section(DEV, PLANS)}
    fails, _ = gate(r, {"schema": suite.SCHEMA, "ids": DEV})
    assert any("NOT flagged" in f for f in fails)


# ------------------------------------------------------------------------------------------------ the full suite


def test_full_suite_is_green_and_dev_only(full: dict[str, Any]) -> None:
    assert full["split"] == "dev" and set(full["ids"]) == set(DEV) and not set(full["ids"]) & set(TEST)
    s = full["agentic"]["summary"]
    assert s["safety_violations"] == 0 and s["task_completion"] == 1.0 and s["misses"] == []
    assert s["known_gaps_open"] == 0 and full["controls"]["all_flagged"]


def test_committed_baseline_passes(full: dict[str, Any]) -> None:
    fails, notes = gate(full, suite.load_baseline())
    assert fails == [] and notes == []


def test_suite_is_deterministic(full: dict[str, Any]) -> None:
    again = run_suite(("agentic", "planner"))
    assert again["agentic"]["summary"] == full["agentic"]["summary"]
    assert again["planner"] == full["planner"]


def test_test_split_cannot_be_opened() -> None:
    with pytest.raises(SuiteError, match="dev split"):
        run_suite(("planner",), [TEST[0]])
    assert not (Path(harness.OPENINGS_LOG).exists() and "agentic" in Path(harness.OPENINGS_LOG).read_text())


def test_unknown_section_is_rejected() -> None:
    with pytest.raises(SuiteError, match="unknown section"):
        run_suite(("nope",))


def test_sections_can_run_alone() -> None:
    r = run_suite(("planner",))
    assert set(r) == {"schema", "split", "ids", "planner"}


# ------------------------------------------------------------------------------------------------ gating


@pytest.fixture()
def base(full: dict[str, Any]) -> dict[str, Any]:
    return suite.baseline_from(full)


def mutated(full: dict[str, Any], fn: Any) -> dict[str, Any]:
    r = copy.deepcopy(full)
    fn(r)
    return r


def test_gate_passes_an_identical_run(full: dict[str, Any], base: dict[str, Any]) -> None:
    assert gate(full, base) == ([], [])


def test_gate_fails_on_any_safety_violation(full: dict[str, Any], base: dict[str, Any]) -> None:
    r = mutated(full, lambda r: r["agentic"]["summary"].update(safety_violations=1, safety_violation_kinds={"x": 1}))
    assert any(f.startswith("SAFETY") for f in gate(r, base)[0])


def test_gate_fails_on_lower_completion_and_each_miss(full: dict[str, Any], base: dict[str, Any]) -> None:
    def f(r: dict[str, Any]) -> None:
        r["agentic"]["summary"]["task_completion"] = 0.9
        r["agentic"]["summary"]["misses"] = [{"scenario": "approve", "incident": "INC-1", "why": ["a", "b"]}]

    fails = gate(mutated(full, f), base)[0]
    assert any("task completion" in x for x in fails) and any("approve/INC-1: a; b" in x for x in fails)


def test_gate_fails_on_efficiency_blowup_only_beyond_tolerance(full: dict[str, Any], base: dict[str, Any]) -> None:
    e = base["agentic"]["efficiency"]["events_per_run"]
    ok = mutated(full, lambda r: r["agentic"]["summary"]["efficiency"].update(events_per_run=e * 1.14))
    bad = mutated(full, lambda r: r["agentic"]["summary"]["efficiency"].update(events_per_run=e * 1.2))
    assert gate(ok, base)[0] == [] and any("events_per_run" in x for x in gate(bad, base)[0])
    bad2 = mutated(full, lambda r: r["agentic"]["summary"]["efficiency"].update(backend_calls_per_run=99))
    assert any("backend_calls_per_run" in x for x in gate(bad2, base)[0])


def test_gate_known_gaps(full: dict[str, Any], base: dict[str, Any]) -> None:
    more = mutated(full, lambda r: r["agentic"]["summary"].update(known_gaps_open=1))
    assert any("known gaps" in x for x in gate(more, base)[0])
    base2 = {**base, "agentic": {**base["agentic"], "known_gaps_open": 2}}
    fewer = mutated(full, lambda r: r["agentic"]["summary"].update(known_gaps_open=1))
    fails, notes = gate(fewer, base2)
    assert fails == [] and any("closed" in n for n in notes)


def test_gate_planner_regressions(full: dict[str, Any], base: dict[str, Any]) -> None:
    iid = MUTATING[0]
    changed = mutated(full, lambda r: r["planner"]["commands"].update({iid: "list-services"}))
    assert any(f"plan for {iid} changed" in x for x in gate(changed, base)[0])
    fa = mutated(full, lambda r: r["planner"].update(false_alarm_mutations=["INC-X"]))
    assert any("false alarm" in x for x in gate(fa, base)[0])


def test_gate_quality_sections_use_a_tolerance(full: dict[str, Any], base: dict[str, Any]) -> None:
    f1 = base["diagnosis"]["root_cause_f1"]
    within = mutated(full, lambda r: r["diagnosis"].update(root_cause_f1=f1 - 0.019))
    below = mutated(full, lambda r: r["diagnosis"].update(root_cause_f1=f1 - 0.03))
    assert gate(within, base)[0] == [] and any("diagnosis.root_cause_f1" in x for x in gate(below, base)[0])
    mem = mutated(full, lambda r: r["memory"].update(combined_accuracy=0.0))
    assert any("memory.combined_accuracy" in x for x in gate(mem, base)[0])
    ret = mutated(full, lambda r: r["retrieval"]["log"].update(mrr=0.1))
    assert any("retrieval.log.mrr" in x for x in gate(ret, base)[0])
    crash = mutated(full, lambda r: r["diagnosis"].update(crashed=1))
    assert any("crashed" in x for x in gate(crash, base)[0])


def test_gate_rejects_a_foreign_baseline_and_a_changed_incident_set(full: dict[str, Any], base: dict[str, Any]) -> None:
    assert "schema" in gate(full, {**base, "schema": 0})[0][0]
    assert any("incident set" in x for x in gate(full, {**base, "ids": ["INC-001"]})[0])


def test_gate_flags_an_unflagged_control(full: dict[str, Any], base: dict[str, Any]) -> None:
    def f(r: dict[str, Any]) -> None:
        r["controls"]["controls"][0]["flagged"] = False

    assert any("NOT flagged" in x for x in gate(mutated(full, f), base)[0])


def test_gate_with_partial_sections_only_checks_what_ran(full: dict[str, Any], base: dict[str, Any]) -> None:
    only = {"schema": 1, "split": "dev", "ids": full["ids"], "planner": full["planner"]}
    assert gate(only, base) == ([], [])


# ------------------------------------------------------------------------------------------------ baseline + cli


def test_load_baseline_errors(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="no baseline"):
        suite.load_baseline(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(SuiteError, match="cannot read"):
        suite.load_baseline(bad)


def test_cli_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0
    text = capsys.readouterr().out
    assert "forged_signal" in text and "auto_approve" in text and "memory" in text


def test_cli_run_passes_and_writes_a_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "r.json"
    assert cli.main(["run", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "RESULT PASS" in text and "SAFETY VIOLATIONS 0" in text and "caught" in text
    assert json.loads(out.read_text())["failures"] == []


def test_cli_run_fails_against_a_stricter_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b = json.loads(suite.BASELINE.read_text())
    b["diagnosis"]["root_cause_f1"] = 0.99
    p = tmp_path / "b.json"
    p.write_text(json.dumps(b))
    assert cli.main(["run", "--only", "diagnosis", "--baseline", str(p), "--json", str(tmp_path / "o.json")]) == 1
    assert "FAIL  diagnosis.root_cause_f1" in capsys.readouterr().out


def test_cli_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    j = str(tmp_path / "o.json")
    assert cli.main(["run", "--only", "bogus", "--json", j]) == 2
    assert cli.main(["run", "--baseline", str(tmp_path / "missing.json"), "--json", j]) == 2
    assert cli.main(["run", "--ids", TEST[0], "--no-gate", "--json", j]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_cli_no_gate_reports_without_comparing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "--only", "planner", "--no-gate", "--json", str(tmp_path / "o.json")]) == 0
    assert "RESULT PASS" in capsys.readouterr().out


def test_update_baseline_needs_the_full_suite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--update-baseline", "--baseline", str(tmp_path / "b.json"), "--json", str(tmp_path / "o.json")]
    assert cli.main(["run", "--only", "planner", *args]) == 2
    assert cli.main(["run", "--ids", DEV[0], *args]) == 2
    assert not (tmp_path / "b.json").exists()
    assert "full suite" in capsys.readouterr().err


def test_update_baseline_writes_and_reproduces(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b = tmp_path / "b.json"
    assert cli.main(["run", "--update-baseline", "--baseline", str(b), "--json", str(tmp_path / "o.json")]) == 0
    assert json.loads(b.read_text()) == json.loads(suite.BASELINE.read_text())
    assert "baseline written" in capsys.readouterr().out


def test_update_baseline_refuses_an_unsafe_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = suite.run_suite

    def unsafe(*a: Any, **k: Any) -> dict[str, Any]:
        r = real(*a, **k)
        r["agentic"]["summary"].update(safety_violations=1, safety_violation_kinds={"x": 1})
        return r

    monkeypatch.setattr(suite, "run_suite", unsafe)
    b = tmp_path / "b.json"
    assert cli.main(["run", "--update-baseline", "--baseline", str(b), "--json", str(tmp_path / "o.json")]) == 2
    assert not b.exists() and "refusing to baseline" in capsys.readouterr().err


def test_backend_availability_and_run_all_filters() -> None:
    assert RecordingBackend().available() == (True, "recording")
    ro = READONLY[0]
    assert suite.run_all([ro], PLANS, only=("silence",)) == []  # a read-only incident has no such scenario
    assert len(suite.run_all([ro], PLANS, only=("read_only",))) == 1
    assert len(suite.run_all([MUTATING[0]], PLANS, only=("silence", "reject"))) == 2
