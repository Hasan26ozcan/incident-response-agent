"""Risk guard, result comparison, LLM-judge and preflight. Fully offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from incident_agent.agents.llm_commander import CommanderReview
from incident_agent.agents.llm_orchestrator import LLMOrchestrator
from incident_agent.agents.llm_react_agent import LLMReActAgent
from incident_agent.agents.risk_guard import apply_risk_guard, risk_floor
from incident_agent.agents.swarm import HierarchicalSwarm
from incident_agent.eval import compare, harness, judge, preflight
from incident_agent.llm import LLMConfigError, LLMResponse, LLMUnavailable, ScriptedClient, ToolCall
from incident_agent.schemas.agent_output import RiskTier
from tests.fakes import FINAL, REVIEW, RoutingFake

GOLD = Path(__file__).resolve().parents[1] / "eval" / "gold"
H, M, L = RiskTier.HIGH, RiskTier.MEDIUM, RiskTier.LOW


# ======================================================================== risk guard
class TestRiskGuard:
    @pytest.mark.parametrize(
        "text",
        [
            "Restart the pods",
            "Rolled back v2.1 and redeployed",
            "Scale out the consumers",
            "Roll back the release",
            "rollback immediately",
            "Cordon and drain the node",
            "Purge old logs",
            "Rotate the credentials",
            "Replace the expired certificate",
            "fail over to the replica",
            "Evict the stuck pods",
            "Re-deploy after the fix",
        ],
    )
    def test_high_risk_actions(self, text):
        assert risk_floor(text)[0] is H

    @pytest.mark.parametrize(
        "text",
        [
            "Revert MAX_RETRIES to 3",
            "Open a ticket with the platform team",
            "Notify the on-call via Slack",
            "Write a post-mortem",
        ],
    )
    def test_medium_risk_actions(self, text):
        assert risk_floor(text)[0] is M

    @pytest.mark.parametrize(
        "text",
        [
            "No action needed; close the alert as a false positive.",
            "No restart needed",
            "Avoid rolling back; keep monitoring",
            "Do not rotate the secret",
            "Without restarting anything, wait for recovery",
            "",
        ],
    )
    def test_negated_or_benign_text_is_low(self, text):
        assert risk_floor(text) == (L, [])

    def test_negation_does_not_leak_across_sentences(self):
        assert risk_floor("No ticket is needed. Restart the pods.")[0] is H

    def test_high_wins_over_medium(self):
        assert risk_floor("Revert the config, then restart the service")[0] is H

    def _diag(self, tier, rec):
        from incident_agent.agents.grounding import ground_diagnosis
        from incident_agent.agents.llm_react_agent import LLMDiagnosis

        out = LLMDiagnosis.model_validate({**FINAL, "risk_tier": tier, "recommendation": rec})
        return ground_diagnosis("t", "INC-001", out, "retry loop")

    def test_guard_only_raises(self):
        d, raised = apply_risk_guard(self._diag("low", "Restart the checkout pods and cap retries."))
        assert raised and d.risk_tier is H
        assert d.reasoning_steps[-1].description.startswith("Risk guard raised the tier low -> high")
        assert [s.step_number for s in d.reasoning_steps] == list(range(1, len(d.reasoning_steps) + 1))

    def test_guard_never_lowers(self):
        for rec in ("No action needed here.", "Open a ticket for the platform team."):
            d, raised = apply_risk_guard(self._diag("high", rec))
            assert not raised and d.risk_tier is H

    def test_guard_leaves_matching_tier_alone(self):
        d, raised = apply_risk_guard(self._diag("medium", "Revert the config change."))
        assert not raised and d.risk_tier is M

    def test_guard_floor_is_never_above_the_human_written_gold_tier(self):
        """Calibration on the 20 gold remediations: the rule must not over-raise and must catch most high-risk ones."""
        order = {"low": 0, "medium": 1, "high": 2}
        high_total = high_hit = 0
        for f in sorted(GOLD.glob("INC-*.json")):
            g = json.loads(f.read_text())
            floor = risk_floor(g["recommended_remediation"])[0].value
            assert order[floor] <= order[g["remediation_risk_tier"]], f"{g['incident_id']}: guard would over-raise"
            if g["remediation_risk_tier"] == "high":
                high_total += 1
                high_hit += floor == "high"
        assert high_hit / high_total >= 0.9

    def test_react_agent_guard_is_off_by_default_and_raises_when_on(self):
        plan = {"hypotheses": ["retry loop"], "steps": [{"tool": "log_overview", "purpose": "mix"}]}
        low = {**FINAL, "risk_tier": "low", "recommendation": "Restart the pods and cap the retry loop."}

        def run(guard: bool):
            script = [
                LLMResponse(content=json.dumps(plan)),
                LLMResponse(content="ready to conclude"),
                LLMResponse(content=json.dumps(low)),
            ]
            return LLMReActAgent(ScriptedClient(script), risk_guard=guard).run("INC-001")

        assert run(False).risk_tier is L and run(True).risk_tier is H

    def test_orchestrator_guard(self):
        low = {**FINAL, "risk_tier": "low", "recommendation": "Roll back the release immediately."}
        assert LLMOrchestrator(RoutingFake(final=low), parallel=False).run("INC-001").risk_tier is L
        assert LLMOrchestrator(RoutingFake(final=low), parallel=False, risk_guard=True).run("INC-001").risk_tier is H

    def test_swarm_guard_changes_the_escalation_decision(self):
        low = {**FINAL, "risk_tier": "low", "confidence": 0.9, "recommendation": "Roll back the release immediately."}
        off = HierarchicalSwarm(RoutingFake(final=low), parallel=False).run("INC-001")
        on = HierarchicalSwarm(RoutingFake(final=low), parallel=False, risk_guard=True).run("INC-001")
        assert off.risk_tier is L and off.escalation_decision == "resolve"
        assert on.risk_tier is H and on.escalation_decision == "escalate"

    def test_guard_also_applies_to_a_commander_revision(self):
        rev = {
            "is_false_alarm": False,
            "root_cause": "A config change at 00:40 exhausted the pool.",
            "category": "db_connection",
            "recommendation": "Restart the service and roll back the pool change.",
            "risk_tier": "low",
            "confidence": 0.9,
            "supporting_quotes": ["ERROR"],
        }
        review = {**REVIEW, "agrees_with_diagnosis": False, "disagreement": "wrong", "revised": rev}
        out = HierarchicalSwarm(
            RoutingFake(review=review, final={**FINAL, "risk_tier": "low"}), parallel=False, risk_guard=True
        ).run("INC-001")
        assert out.risk_tier is H and out.escalation_decision == "escalate"

    def test_harness_cli_flag_is_recorded_and_passed_through(self, tmp_path, monkeypatch):
        seen: dict[str, Any] = {}

        def fake_make(name, temperature=0.0, risk_guard=False):
            seen["guard"] = risk_guard
            return lambda iid: harness.DiagnosisResult(iid, "Unknown", risk_tier="low")

        monkeypatch.setattr(harness, "make_adapter", fake_make)
        monkeypatch.setattr(harness, "RESULTS_DIR", tmp_path)
        harness.main(["--agent", "llm_react", "--ids", "INC-001", "--tag", "g", "--risk-guard"])
        assert seen["guard"] is True
        assert json.loads((tmp_path / "llm_react__g.json").read_text())["meta"]["risk_guard"] is True


# ======================================================================== compare
def _report(name: str, f1: dict[str, float], **summary: Any) -> compare.Run:
    rows = [
        {
            "incident_id": i,
            "category": "c",
            "difficulty": "d",
            "root_cause_f1": v,
            "evidence_recall": v,
            "risk_match": v > 0.5,
            "false_alarm_ok": True,
            "answered": True,
            "error": "",
            "usage": {},
            "degraded": "",
        }
        for i, v in f1.items()
    ]
    s = {
        "n": len(rows),
        "root_cause_f1": sum(f1.values()) / len(f1),
        "evidence_recall": 0.0,
        "risk_accuracy": 0.0,
        "false_alarm_accuracy": 1.0,
        "answered_rate": 1.0,
        "crashed": 0,
        "degraded": 0,
        "llm_calls": 10,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        **summary,
    }
    return compare.Run(name, {"meta": {"complete": True}, "summary": s, "incidents": rows})


IDS = [f"INC-{i:03d}" for i in range(1, 11)]


class TestCompare:
    def test_bootstrap_is_deterministic_and_brackets_the_mean(self):
        d = [0.1, 0.2, 0.15, 0.05, 0.3, 0.1, 0.2, 0.25, 0.1, 0.15]
        a, b = compare.paired_bootstrap(d), compare.paired_bootstrap(d)
        assert a == b and a[0] <= sum(d) / len(d) <= a[1] and a[0] > 0

    def test_identical_runs_are_not_significant(self):
        base = _report("a", dict.fromkeys(IDS, 0.4))
        c = compare.compare_to_baseline(base, _report("b", dict.fromkeys(IDS, 0.4)), "root_cause_f1")
        assert c["mean_delta"] == 0 and c["ties"] == 10 and not c["significant"]

    def test_consistent_improvement_is_significant_with_wins_counted(self):
        base = _report("a", dict.fromkeys(IDS, 0.2))
        better = _report("b", {i: 0.5 + (0.01 * k) for k, i in enumerate(IDS)})
        c = compare.compare_to_baseline(base, better, "root_cause_f1")
        assert c["significant"] and c["wins"] == 10 and c["losses"] == 0 and c["ci"][0] > 0

    def test_noisy_mixed_result_is_not_significant(self):
        base = _report("a", dict.fromkeys(IDS, 0.4))
        other = _report("b", {i: 0.4 + (0.3 if k % 2 else -0.3) for k, i in enumerate(IDS)})
        c = compare.compare_to_baseline(base, other, "root_cause_f1")
        assert not c["significant"] and c["wins"] == 5 and c["losses"] == 5

    def test_only_common_incidents_are_compared(self):
        base = _report("a", dict.fromkeys(IDS, 0.2))
        other = _report("b", dict.fromkeys(IDS[:4], 0.9))
        assert compare.compare_to_baseline(base, other, "root_cause_f1")["n"] == 4

    def test_empty_overlap_is_safe(self):
        c = compare.compare_to_baseline(_report("a", {"INC-001": 0.1}), _report("b", {"INC-002": 0.1}), "root_cause_f1")
        assert c["n"] == 0 and c["mean_delta"] == 0 and not c["significant"]
        assert compare.paired_bootstrap([]) == (0.0, 0.0)

    def test_render_text_and_markdown_with_cost_and_small_n_note(self):
        base = _report("a", dict.fromkeys(IDS, 0.2))
        other = _report("b", dict.fromkeys(IDS, 0.6), llm_calls=30)
        text = compare.render([base, other])
        assert "SIGNIFICANT" in text and "3.0x the LLM calls" in text and "n=10" in text and "indicative" in text
        md = compare.render([base, other], markdown=True)
        assert md.startswith("| run |") and "|---|" in md

    def test_load_run_by_stem_by_path_and_missing(self, tmp_path):
        rep = {"meta": {"complete": True}, "summary": {"n": 1}, "incidents": []}
        (tmp_path / "x__t.json").write_text(json.dumps(rep))
        assert compare.load_run("x__t", tmp_path).name == "x__t"
        assert compare.load_run(str(tmp_path / "x__t.json")).name == "x__t"
        with pytest.raises(FileNotFoundError):
            compare.load_run("nope", tmp_path)

    def test_incomplete_run_warns_and_judge_scores_are_loaded(self, tmp_path, capsys):
        rep = {"meta": {"complete": False}, "summary": {"n": 1}, "incidents": []}
        (tmp_path / "x__t.json").write_text(json.dumps(rep))
        (tmp_path / "judge__t.json").write_text(json.dumps({"incidents": [{"incident_id": "INC-001", "score": 0.5}]}))
        run = compare.load_run("x__t", tmp_path)
        assert "incomplete" in capsys.readouterr().out and run.judge == {"INC-001": 0.5}

    def test_render_includes_judge_column_when_available(self):
        a, b = _report("a", dict.fromkeys(IDS, 0.2)), _report("b", dict.fromkeys(IDS, 0.3))
        a.judge, b.judge = {"INC-001": 0.0}, {"INC-001": 1.0}
        assert "judge" in compare.render([a, b]).splitlines()[0]

    def test_main_prints(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(compare, "RESULTS_DIR", tmp_path)
        for n, v in (("a__x", 0.2), ("b__x", 0.6)):
            r = _report(n, dict.fromkeys(IDS, v))
            (tmp_path / f"{n}.json").write_text(json.dumps(r.report))
        assert compare.main(["a__x", "b__x"]) == 0
        assert "vs baseline a__x" in capsys.readouterr().out


# ======================================================================== judge
class JudgeFake:
    model = "judge-model"

    def __init__(self, verdict="correct", fail=None):
        self.verdict, self.fail, self.prompts = verdict, fail, []

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        self.prompts.append(messages[-1]["content"])
        if self.fail:
            raise self.fail
        body = {
            "verdict": self.verdict,
            "mechanism_match": True,
            "trigger_match": True,
            "reason": "same mechanism and trigger",
        }
        return LLMResponse(content=json.dumps(body))


def _traces(tmp_path, tag="t", diag=True):
    d = tmp_path / tag
    d.mkdir()
    (d / "INC-001.json").write_text(
        json.dumps({"diagnosis": {"root_cause": "Retry loop pinned CPU"}} if diag else {"x": 1})
    )
    return tmp_path


class TestJudge:
    def test_judge_never_sees_evidence_or_agent_identity(self):
        fake = JudgeFake()
        gold = json.loads((GOLD / "INC-001.json").read_text())
        v = judge.judge_one(fake, gold, "candidate text")
        assert v.verdict == "correct"
        assert "candidate text" in fake.prompts[0] and gold["root_cause"] in fake.prompts[0]
        assert gold["evidence"][0] not in fake.prompts[0] and "llm_" not in fake.prompts[0]

    @pytest.mark.parametrize(("verdict", "score"), [("correct", 1.0), ("partially_correct", 0.5), ("incorrect", 0.0)])
    def test_run_scores(self, tmp_path, verdict, score):
        rep = judge.judge_run(JudgeFake(verdict), "t", trace_dir=_traces(tmp_path))
        assert rep["summary"]["judge_score"] == score and rep["incidents"][0]["verdict"] == verdict

    def test_crashed_incident_scores_zero_without_calling_the_model(self, tmp_path):
        fake = JudgeFake()
        rep = judge.judge_run(fake, "t", trace_dir=_traces(tmp_path, diag=False))
        assert rep["incidents"][0]["score"] == 0.0 and fake.prompts == []

    def test_id_filter_and_missing_traces(self, tmp_path):
        assert judge.judge_run(JudgeFake(), "t", ids=["INC-002"], trace_dir=_traces(tmp_path))["incidents"] == []
        with pytest.raises(FileNotFoundError):
            judge.judge_run(JudgeFake(), "missing", trace_dir=tmp_path)

    def _env(self, monkeypatch, **env):
        monkeypatch.setattr(judge, "_load_dotenv", lambda path=None: None)
        for k in ("JUDGE_MODEL", "JUDGE_PROVIDER", "GROQ_API_KEY", "CEREBRAS_API_KEY", "GROQ_MODEL"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    def test_builder_errors(self, monkeypatch):
        self._env(monkeypatch)
        with pytest.raises(LLMUnavailable, match="JUDGE_MODEL"):
            judge.build_judge_client()
        self._env(monkeypatch, JUDGE_MODEL="m", JUDGE_PROVIDER="nope")
        with pytest.raises(LLMUnavailable, match="JUDGE_PROVIDER"):
            judge.build_judge_client()
        self._env(monkeypatch, JUDGE_MODEL="m")
        with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
            judge.build_judge_client()

    def test_same_model_is_refused_unless_allowed(self, monkeypatch, tmp_path):
        self._env(monkeypatch, JUDGE_MODEL="openai/gpt-oss-120b", GROQ_API_KEY="k", LLM_CACHE_DIR=str(tmp_path))
        with pytest.raises(judge.SameModelError):
            judge.build_judge_client()
        assert judge.build_judge_client(allow_same=True).model == "openai/gpt-oss-120b"

    def test_different_model_builds_for_each_provider(self, monkeypatch, tmp_path):
        self._env(monkeypatch, JUDGE_MODEL="llama-x", GROQ_API_KEY="k", LLM_CACHE_DIR=str(tmp_path))
        assert judge.build_judge_client().model == "llama-x"
        self._env(
            monkeypatch,
            JUDGE_MODEL="c-model",
            JUDGE_PROVIDER="cerebras",
            CEREBRAS_API_KEY="k",
            LLM_CACHE_DIR=str(tmp_path),
        )
        assert judge.build_judge_client().model == "c-model"

    def test_main_writes_the_result_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(judge, "build_judge_client", lambda **k: JudgeFake())
        monkeypatch.setattr(judge, "TRACE_DIR", _traces(tmp_path))
        monkeypatch.setattr(judge, "RESULTS_DIR", tmp_path / "res")
        assert judge.main(["--tag", "t"]) == 0
        out = json.loads((tmp_path / "res" / "judge__t.json").read_text())
        assert out["summary"]["judge_score"] == 1.0 and "judge_score=1.0" in capsys.readouterr().out

    def test_main_reports_a_startup_problem_without_a_traceback(self, monkeypatch, capsys):
        def boom(**kw):
            raise judge.SameModelError("same family")

        monkeypatch.setattr(judge, "build_judge_client", boom)
        assert judge.main(["--tag", "t"]) == 1 and "cannot start judge" in capsys.readouterr().out

    def test_main_with_split_filters_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr(judge, "build_judge_client", lambda **k: JudgeFake())
        monkeypatch.setattr(judge, "TRACE_DIR", _traces(tmp_path))
        monkeypatch.setattr(judge, "RESULTS_DIR", tmp_path / "res")
        judge.main(["--tag", "t", "--split", "test"])  # INC-001 is a dev incident
        assert json.loads((tmp_path / "res" / "judge__t.json").read_text())["incidents"] == []


# ======================================================================== preflight
class CapFake:
    model = "cap-fake"

    def __init__(self, *, text_only=False, bad_json=False, fail=None):
        self.text_only, self.bad_json, self.fail = text_only, bad_json, fail

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        if self.fail:
            raise self.fail
        if tools:
            if self.text_only:
                return LLMResponse(content="I would call it")
            return LLMResponse(tool_calls=[ToolCall(id="1", name="log_overview", arguments={})])
        if response_schema is not None:
            return LLMResponse(content="nope" if self.bad_json else '{"value": 7}')
        return LLMResponse(content="OK", usage={"prompt_tokens": 5, "completion_tokens": 1})


class TestPreflight:
    def test_all_checks_pass(self):
        res = preflight.run_checks(CapFake())
        assert [r[0] for r in res] == ["plain chat", "tool calling", "structured output"] and all(r[1] for r in res)

    def test_text_only_model_fails_tool_calling_with_a_useful_message(self):
        res = {r[0]: r for r in preflight.run_checks(CapFake(text_only=True))}
        assert not res["tool calling"][1] and "tool calling" in res["tool calling"][2] and res["plain chat"][1]

    def test_invalid_structured_output_fails(self):
        assert not {r[0]: r[1] for r in preflight.run_checks(CapFake(bad_json=True))}["structured output"]

    def test_provider_errors_are_reported_not_raised(self):
        for exc in (LLMUnavailable("down"), LLMConfigError("bad key")):
            assert not any(r[1] for r in preflight.run_checks(CapFake(fail=exc)))

    def test_structured_check_reports_the_mode_in_use(self):
        c = CapFake()
        c._json_object_mode = True  # type: ignore[attr-defined]
        assert "json_object" in {r[0]: r[2] for r in preflight.run_checks(c)}["structured output"]

    def test_main_exit_codes(self, monkeypatch, capsys):
        monkeypatch.setattr(preflight, "_load_dotenv", lambda path=None: None)
        monkeypatch.setattr(preflight, "build_client", lambda **k: CapFake())
        assert preflight.main([]) == 0 and "READY" in capsys.readouterr().out
        monkeypatch.setattr(preflight, "build_client", lambda **k: CapFake(text_only=True))
        assert preflight.main([]) == 1 and "NOT READY" in capsys.readouterr().out

    def test_main_reports_a_missing_key(self, monkeypatch, capsys):
        def boom(**k):
            raise LLMUnavailable("GROQ_API_KEY not found")

        monkeypatch.setattr(preflight, "_load_dotenv", lambda path=None: None)
        monkeypatch.setattr(preflight, "build_client", boom)
        assert preflight.main([]) == 1 and "cannot build client" in capsys.readouterr().out

    def test_main_judge_flag_checks_the_judge_too(self, monkeypatch, capsys):
        monkeypatch.setattr(preflight, "_load_dotenv", lambda path=None: None)
        monkeypatch.setattr(preflight, "build_client", lambda **k: CapFake())
        monkeypatch.setattr(judge, "build_judge_client", lambda **k: CapFake())
        assert preflight.main(["--judge"]) == 0
        assert "judge model" in capsys.readouterr().out


def test_commander_review_schema_untouched_by_guard():  # guard must not change the LLM-facing schemas
    assert "revised" in CommanderReview.model_json_schema()["properties"]
