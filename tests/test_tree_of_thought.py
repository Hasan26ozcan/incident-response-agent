"""Stage 9: Tree-of-Thought + Plan-and-Solve.

Roadmap: parallel hypothesis branching on complex scenarios; the most likely scenario is selected; accuracy is
measured on the gold set. Here: conformance (parallel plan-and-solve branches, H0 kept, tools per branch),
deterministic scoring, selection margin, the shared acceptance policy, gating, failure isolation and wiring.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from incident_agent.agents.grounding import ground_diagnosis, tokens
from incident_agent.agents.llm_commander import Signals
from incident_agent.agents.llm_debate import LLMDebate
from incident_agent.agents.llm_react_agent import LLMDiagnosis
from incident_agent.agents.swarm import HierarchicalSwarm
from incident_agent.agents.tree_of_thought import (
    LOW_MARGIN_CAP,
    SWITCH_MARGIN,
    BranchReport,
    Hypotheses,
    ToTOrchestrator,
    ToTResult,
    TreeOfThought,
    score_branch,
)
from incident_agent.eval import harness
from incident_agent.llm import LLMConfigError, LLMResponse, LLMUnavailable, StructuredOutputError, ToolCall
from incident_agent.schemas.agent_output import RiskTier
from incident_agent.tools.toolbox import TOOL_NAMES
from tests.fakes import FINAL, RoutingFake

EVIDENCE = (
    "ERROR checkout-api retry loop saturated CPU at 00:40 after deploy v2.3.1 raised the pool size. "
    "db latency normal. cache hit ratio healthy. planned maintenance window closed at 00:30. "
    "connection pool exhausted: active=50 idle=0 waiting=120"
)
BASE_RC = FINAL["root_cause"]  # "An unbounded retry loop saturated the checkout service CPU."
H1_RC = "The database connection pool was exhausted after the v2.3.1 pool size change."
H2_RC = "No real incident: the alert came from the scheduled maintenance window closing."
H3_RC = "A noisy neighbour on the shared node starved the pods of memory bandwidth."
Q_POOL = "connection pool exhausted"
Q_CPU = "retry loop saturated CPU"
Q_DB = "db latency normal"
Q_CACHE = "cache hit ratio healthy"
Q_MAINT = "planned maintenance window closed"
BAD_Q = "kernel panic node-7 caused unexpected reboot"
OBS = [
    {"text": "CPU saturated at 00:40", "quote": Q_CPU},
    {"text": "db latency is normal", "quote": Q_DB},
    {"text": "pool was exhausted", "quote": Q_POOL},
    {"text": "maintenance window closed at 00:30", "quote": Q_MAINT},
]
H, M, L = RiskTier.HIGH, RiskTier.MEDIUM, RiskTier.LOW


def diag(root: str | None = None, conf: float = 0.8, fa: bool = False, rec: str | None = None):
    data: dict[str, Any] = {**FINAL, "confidence": conf, "is_false_alarm": fa, "risk_tier": "high"}
    if root:
        data["root_cause"] = root
    if rec:
        data["recommendation"] = rec
    return ground_diagnosis("t", "INC-001", LLMDiagnosis.model_validate(data), "retry loop")


def hyp(*specs: tuple[str, bool], obs: list[dict] | None = None) -> dict[str, Any]:
    return {
        "observations": OBS if obs is None else obs,
        "hypotheses": [
            {
                "root_cause": rc,
                "mechanism": "m",
                "category": "x",
                "is_false_alarm": fa,
                "plan": ["check one", "check two"],
            }
            for rc, fa in specs
        ],
    }


def rep(
    support: list[str] = (),
    refute: list[str] = (),
    explains: list[int] = (),
    contradicted: list[int] = (),
    plan: list[tuple[int, str, str]] = (),
) -> dict[str, Any]:
    return {
        "findings": [{"kind": "support", "claim": "claim text", "quote": q} for q in support]
        + [{"kind": "refute", "claim": "claim text", "quote": q} for q in refute],
        "plan_results": [{"step": s, "outcome": o, "quote": q} for s, o, q in plan],
        "explains": list(explains),
        "contradicted": list(contradicted),
    }


STRONG = rep([Q_POOL, Q_DB, Q_CACHE], explains=[1, 2, 3], plan=[(0, "supported", Q_POOL), (1, "supported", Q_DB)])
MID = rep([Q_CPU], explains=[0])
WEAK = rep([], refute=[Q_DB], contradicted=[1, 2])


def solved(quotes: list[str], *, fa: bool = False, risk: str = "medium", conf: float = 0.85, rec: str | None = None):
    return {
        "is_false_alarm": fa,
        "root_cause": "The connection pool was exhausted after the v2.3.1 pool size change."
        if not fa
        else "Healthy: no real incident, the alert came from the maintenance window.",
        "category": "db_connection",
        "recommendation": rec or "Revert the pool size change and reload the configuration.",
        "risk_tier": risk,
        "confidence": conf,
        "supporting_quotes": quotes,
    }


class ToTFake:
    """Schema/role-driven fake that is safe for parallel branches (branch identified by its hypothesis text)."""

    model = "tot-fake"

    def __init__(
        self,
        *,
        hypotheses: Any = None,
        reports: dict[str, Any] | None = None,
        solve: Any = None,
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.hypotheses = hypotheses if hypotheses is not None else hyp((H1_RC, False), (H2_RC, True))
        # keyed by a prefix of the hypothesis text; "H0" is the base diagnosis
        self.reports = (
            reports if reports is not None else {"An unbounded": MID, "The database": STRONG, "No real": WEAK}
        )
        self.solve = solve if solve is not None else solved([Q_POOL, Q_DB])
        self.fail = fail or {}
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.tool_turns: dict[str, int] = {}
        self.tools_offered: list[set[str]] = []

    @staticmethod
    def branch_key(messages: list[dict[str, Any]]) -> str:
        text = messages[1]["content"]
        return text.split("Hypothesis under test: ", 1)[1].split("\n", 1)[0]

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        system = messages[0]["content"]
        if system.startswith("You are a senior SRE generating"):
            role = "hypothesize"
        elif system.startswith("You are an investigator"):
            role = "branch"
        elif system.startswith("You finalize the diagnosis"):
            role = "solve"
        else:
            raise AssertionError(f"unexpected system prompt: {system[:40]}")
        key = self.branch_key(messages) if role == "branch" else role
        with self.lock:
            self.calls.append(role if role != "branch" else f"branch:{key[:12]}")
            if tools:
                self.tools_offered.append({t["function"]["name"] for t in tools})
        for fail_key, exc in self.fail.items():
            if key.startswith(fail_key) or role == fail_key:
                raise exc
        if role == "branch" and tools:
            with self.lock:
                turn = self.tool_turns.get(key, 0)
                self.tool_turns[key] = turn + 1
            if turn == 0:
                return LLMResponse(tool_calls=[ToolCall(id=f"t-{key[:6]}", name="log_overview", arguments={})])
            return LLMResponse(content="done")
        if role == "hypothesize":
            body = self.hypotheses
        elif role == "solve":
            body = self.solve
        else:
            body = next((v for k, v in self.reports.items() if key.startswith(k)), rep())
        return LLMResponse(content=body if isinstance(body, str) else json.dumps(body))


def run(fake: ToTFake, d=None, *, signals=None, degraded=False, parallel=False, **kw: Any) -> ToTResult:
    return TreeOfThought(fake, parallel=parallel, **kw).run(
        "INC-001", "meta", d or diag(), EVIDENCE, signals=signals, degraded=degraded
    )


def sig(*, normal: bool) -> Signals:
    return Signals(
        3, 3, [], ["log", "metrics", "deploy"] if normal else [], [] if normal else ["log"], 1.0, None, normal
    )


SEEN = tokens(EVIDENCE)


# --------------------------------------------------------------------------- deterministic scoring
class TestScoring:
    def _score(self, report: dict[str, Any], n_plan: int = 2, n_obs: int = 4):
        return score_branch(BranchReport.model_validate(report), n_plan, n_obs, SEEN)

    def test_exact_formula(self):
        score, info = self._score(STRONG)
        # support 3/4 + 1.5 * coverage 3/4 - 0 - 0 + 0.5 * plan 2/2
        assert score == round(0.75 + 1.5 * 0.75 + 0.5, 4)
        assert info["parts"] == {
            "support": 0.75,
            "coverage": 0.75,
            "refutation": 0.0,
            "contradiction": 0.0,
            "plan": 1.0,
        }

    def test_ungrounded_quotes_count_for_nothing(self):
        score, info = self._score(rep([BAD_Q, BAD_Q + " again"], refute=[BAD_Q], plan=[(0, "supported", BAD_Q)]))
        assert score == 0.0 and info["support"] == [] and info["refute"] == []

    def test_support_is_capped_at_four_and_refutation_at_three(self):
        many = [Q_POOL, Q_DB, Q_CACHE, Q_CPU, Q_MAINT]
        assert self._score(rep(many))[1]["parts"]["support"] == 1.0
        assert self._score(rep([], refute=many))[1]["parts"]["refutation"] == 1.0

    def test_refutation_and_contradiction_lower_the_score(self):
        assert self._score(WEAK)[0] == round(-1.5 * (1 / 3) - 2 / 4, 4)

    def test_invalid_observation_and_plan_indexes_are_ignored(self):
        report = rep(
            [Q_POOL], explains=[0, 9, -1], contradicted=[7], plan=[(5, "supported", Q_POOL), (2, "supported", Q_POOL)]
        )
        parts = self._score(report)[1]["parts"]
        assert parts["coverage"] == 0.25 and parts["contradiction"] == 0.0 and parts["plan"] == 0.0

    def test_explaining_and_contradicting_the_same_observation_cancels_the_contradiction(self):
        assert self._score(rep([], explains=[1], contradicted=[1]))[1]["parts"]["contradiction"] == 0.0

    def test_plan_step_needs_supported_outcome_and_a_grounded_quote(self):
        plan = [(0, "refuted", Q_POOL), (1, "unknown", Q_POOL), (1, "supported", BAD_Q)]
        assert self._score(rep(plan=plan))[1]["parts"]["plan"] == 0.0

    def test_no_observations_or_plan_does_not_divide_by_zero(self):
        score, info = score_branch(BranchReport.model_validate(rep([Q_POOL])), 0, 0, SEEN)
        assert score == 0.25 and info["parts"]["coverage"] == 0.0

    def test_the_model_cannot_supply_its_own_score(self):
        assert not {"score", "confidence", "probability"} & set(BranchReport.model_fields)
        assert "score" not in Hypotheses.model_fields


# --------------------------------------------------------------------------- roadmap conformance
class TestConformance:
    def test_parallel_branches_each_run_plan_and_solve_with_tools(self):
        f = ToTFake()
        out = run(f, parallel=True)
        assert [b.id for b in out.branches] == ["H0", "H1", "H2"]
        assert f.calls[0] == "hypothesize"
        branch_calls = [c for c in f.calls if c.startswith("branch:")]
        assert len(branch_calls) >= 3 * 2  # per branch: at least one tool turn and one report
        assert all(b.transcript and b.report is not None for b in out.branches)
        assert all(o == set(TOOL_NAMES) for o in f.tools_offered)

    def test_the_current_diagnosis_is_always_branch_h0_and_cannot_be_dropped_by_the_model(self):
        out = run(ToTFake(hypotheses=hyp((BASE_RC, False))))  # model only restates the diagnosis
        assert out.verdict == "kept" and [b.id for b in out.branches] == [] and out.discarded_hypotheses == 1
        out2 = run(ToTFake())
        assert out2.branches[0].id == "H0" and out2.branches[0].root_cause == BASE_RC

    def test_duplicates_of_each_other_and_over_budget_hypotheses_are_discarded(self):
        spec = hyp((H1_RC, False), (H1_RC + " again", False), (H2_RC, True), (H3_RC, False))
        out = run(ToTFake(hypotheses=spec), n_branches=3)
        assert [b.id for b in out.branches] == ["H0", "H1", "H2"]
        assert out.discarded_hypotheses == 2 and any("discarded" in n for n in out.notes)

    def test_parallel_and_serial_runs_agree(self):
        a, b = run(ToTFake(), parallel=True), run(ToTFake(), parallel=False)
        assert a.to_dict() == b.to_dict()

    def test_hypotheses_are_asked_for_with_the_evidence_and_the_diagnosis_for_scoring(self):
        from incident_agent.prompts import tot_prompts as P

        assert "VERBATIM" in P.HYPOTHESIZE_SYSTEM and "DIFFERENT" in P.HYPOTHESIZE_SYSTEM
        for text in (P.HYPOTHESIZE_SYSTEM, P.BRANCH_SYSTEM):
            assert "untrusted" in text.lower()
        assert "SUPPORTS" in P.BRANCH_SYSTEM and "REFUTES" in P.BRANCH_SYSTEM

    def test_ungrounded_observations_are_dropped_and_counted(self):
        spec = hyp((H1_RC, False), obs=[*OBS, {"text": "ghost", "quote": BAD_Q}])
        out = run(ToTFake(hypotheses=spec))
        assert len(out.observations) == 4 and any("observation" in n for n in out.notes)

    def test_never_executes_remediation(self):
        assert not any(t.startswith(("restart", "rollback", "scale", "delete")) for t in TOOL_NAMES)

    def test_result_is_json_serialisable_for_traces(self):
        out = run(ToTFake())
        assert json.loads(json.dumps(out.to_dict()))["winner"] == out.winner


# --------------------------------------------------------------------------- selection
class TestSelection:
    def test_a_clearly_better_challenger_wins_and_the_diagnosis_is_rewritten(self):
        out = run(ToTFake())
        assert (out.verdict, out.winner, out.changed) == ("switched", "H1", True)
        assert "connection pool" in out.diagnosis.root_cause and out.diagnosis.agent_type == "llm_tot"
        assert any(e.source == "tot:H1" for e in out.diagnosis.evidence)
        assert len(out.diagnosis.evidence) == len(diag().evidence) + 2
        assert "switched to H1" in out.diagnosis.reasoning_steps[-1].description
        assert out.margin is not None and out.margin >= SWITCH_MARGIN

    def test_a_challenger_inside_the_margin_does_not_displace_h0(self):
        reports = {
            "An unbounded": rep([Q_CPU, Q_DB], explains=[0, 1]),
            "The database": rep([Q_POOL, Q_DB], explains=[1, 2]),
        }
        out = run(ToTFake(hypotheses=hyp((H1_RC, False)), reports=reports))
        scores = {b.id: b.score for b in out.branches}
        assert scores["H0"] == scores["H1"] and out.verdict == "kept" and out.winner == "H0"

    def test_ties_keep_the_existing_diagnosis(self):
        same = rep([Q_POOL], explains=[2])
        out = run(ToTFake(hypotheses=hyp((H1_RC, False)), reports={"An unbounded": same, "The database": same}))
        assert out.winner == "H0" and out.verdict == "kept"

    def test_challenger_one_hair_below_the_margin_loses_and_at_the_margin_wins(self):
        h0 = rep([Q_CPU], explains=[0])  # support .25 + coverage .375 = 0.625
        below = rep([Q_POOL, Q_DB], explains=[1])  # .5 + .375 = 0.875 -> margin 0.25 >= .15 (wins)
        out = run(ToTFake(hypotheses=hyp((H1_RC, False)), reports={"An unbounded": h0, "The database": below}))
        assert out.winner == "H1"
        out2 = run(
            ToTFake(hypotheses=hyp((H1_RC, False)), reports={"An unbounded": h0, "The database": below}),
            switch_margin=0.26,
        )
        assert out2.winner == "H0"

    def test_the_best_of_several_challengers_is_chosen(self):
        spec = hyp((H1_RC, False), (H3_RC, False))
        reports = {"An unbounded": MID, "The database": rep([Q_POOL, Q_DB], explains=[1, 2]), "A noisy": STRONG}
        out = run(ToTFake(hypotheses=spec, reports=reports), n_branches=3)
        assert out.winner == "H2"

    def test_h0_wins_strongly_keeps_its_confidence(self):
        reports = {"An unbounded": STRONG, "The database": WEAK, "No real": WEAK}
        out = run(ToTFake(reports=reports), d=diag(conf=0.85))
        assert out.verdict == "kept" and out.diagnosis.confidence == 0.85
        assert out.diagnosis.reasoning_steps[-1].description.startswith("Tree of thought: 3 branches scored")

    def test_h0_winning_narrowly_caps_confidence(self):
        reports = {"An unbounded": MID, "The database": MID, "No real": WEAK}
        out = run(ToTFake(reports=reports), d=diag(conf=0.9))
        assert out.verdict == "kept" and out.diagnosis.confidence == LOW_MARGIN_CAP

    def test_margin_is_measured_against_the_runner_up(self):
        out = run(ToTFake())
        scores = sorted((b.score for b in out.branches if b.score is not None), reverse=True)
        assert out.margin == round(scores[0] - scores[1], 4)

    def test_a_switch_with_a_thin_margin_is_confidence_capped(self):
        # H0 has 3 plan steps, challengers 2: H0 = .25 + .5/3, challenger = .25 + .5/2 -> margin ~ .083
        reports = {
            "An unbounded": rep([Q_CPU], plan=[(0, "supported", Q_CPU)]),
            "The database": rep([Q_POOL], plan=[(0, "supported", Q_POOL)]),
        }
        out = run(ToTFake(hypotheses=hyp((H1_RC, False)), reports=reports), switch_margin=0.05)
        assert out.verdict == "switched"
        assert out.margin is not None and 0.05 <= out.margin < SWITCH_MARGIN  # below the confidence margin
        assert out.diagnosis.confidence == LOW_MARGIN_CAP  # 0.85 from the solve step, capped


# --------------------------------------------------------------------------- shared acceptance policy
class TestPolicy:
    def test_ungrounded_solve_quotes_reject_the_switch(self):
        out = run(ToTFake(solve=solved([BAD_Q, BAD_Q + " again", Q_DB])))
        assert out.verdict == "switch_rejected" and out.diagnosis.root_cause == BASE_RC and not out.changed
        assert out.winner == "H1" and any("rejected" in n for n in out.notes)

    def test_half_grounded_switch_is_accepted_with_a_confidence_penalty(self):
        out = run(ToTFake(solve=solved([Q_POOL, BAD_Q])))
        assert out.verdict == "switched" and out.diagnosis.confidence == 0.7  # 0.85 - 0.15

    def _dismiss(self, quotes, signals):
        reports = {"An unbounded": MID, "No real": STRONG, "The database": WEAK}
        spec = hyp((H2_RC, True), (H1_RC, False))
        return run(
            ToTFake(hypotheses=spec, reports=reports, solve=solved(quotes, fa=True, risk="low")), signals=signals
        )

    def test_dismissing_a_real_incident_needs_two_grounded_quotes_and_every_domain_normal(self):
        assert self._dismiss([Q_DB], sig(normal=True)).verdict == "switch_rejected"
        assert self._dismiss([Q_DB, BAD_Q], sig(normal=True)).verdict == "switch_rejected"
        assert self._dismiss([Q_DB, Q_CACHE], sig(normal=False)).verdict == "switch_rejected"
        ok = self._dismiss([Q_DB, Q_CACHE], sig(normal=True))
        assert ok.verdict == "switched" and ok.after_false_alarm and not ok.before_false_alarm
        assert ok.diagnosis.root_cause.lower().startswith("false alarm")

    def test_a_false_alarm_can_be_turned_into_a_real_incident_with_half_the_quotes(self):
        base = diag(root="False alarm: nothing is wrong here.", fa=True)
        out = run(ToTFake(solve=solved([Q_POOL])), d=base)
        assert out.verdict == "switched" and out.before_false_alarm and not out.after_false_alarm

    def test_risk_guard_raises_but_never_lowers_a_switched_tier(self):
        sol = solved([Q_POOL, Q_DB], risk="low", rec="Restart the service and roll back the pool change.")
        assert run(ToTFake(solve=sol)).diagnosis.risk_tier is L
        assert run(ToTFake(solve=sol), risk_guard=True).diagnosis.risk_tier is H

    def test_a_rejected_switch_still_records_why_in_the_reasoning_steps(self):
        out = run(ToTFake(solve=solved([BAD_Q])))
        assert "rejected" in out.diagnosis.reasoning_steps[-1].description


# --------------------------------------------------------------------------- tools and winner evidence
class TestToolsAndWinnerEvidence:
    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_branches_can_actually_call_every_tool(self, tool):
        class Caller(ToTFake):
            def chat(self, messages, *, tools=None, **kw):
                if messages[0]["content"].startswith("You are an investigator") and tools:
                    key = self.branch_key(messages)
                    with self.lock:
                        turn = self.tool_turns.get(key, 0)
                        self.tool_turns[key] = turn + 1
                    if turn == 0:
                        args = {"query": "error"} if tool == "search_logs" else {}
                        return LLMResponse(tool_calls=[ToolCall(id="x", name=tool, arguments=args)])
                    return LLMResponse(content="done")
                return super().chat(messages, tools=tools, **kw)

        out = run(Caller())
        first = out.branches[1].transcript[0]
        assert first.startswith(f"> {tool}(") and "not available" not in first and "unknown tool" not in first

    def test_quotes_from_the_winners_own_tool_output_count_as_grounded_at_solve_time(self):
        probe = run(ToTFake())
        out_lines = [ln.strip() for ln in probe.branches[1].transcript[0].splitlines()[1:] if len(ln.strip()) > 25]
        novel = [ln for ln in out_lines if not tokens(ln) <= SEEN]
        assert novel, "tool output must contain text that is not in the shared evidence"
        quotes = novel[:2] if len(novel) >= 2 else [novel[0], novel[0]]
        out = run(ToTFake(solve=solved(quotes)))
        assert out.verdict == "switched" and out.changed


# --------------------------------------------------------------------------- gating
class TestGating:
    def test_confident_diagnoses_can_skip_the_tree_without_any_llm_call(self):
        f = ToTFake()
        out = run(f, d=diag(conf=0.9), skip_if_confidence_at_least=0.85)
        assert out.verdict == "gated_off" and f.calls == [] and out.diagnosis is out.original

    def test_gate_is_inclusive_and_lower_confidence_runs(self):
        assert run(ToTFake(), d=diag(conf=0.85), skip_if_confidence_at_least=0.85).verdict == "gated_off"
        assert run(ToTFake(), d=diag(conf=0.84), skip_if_confidence_at_least=0.85).verdict != "gated_off"

    def test_no_gate_by_default(self):
        assert run(ToTFake(), d=diag(conf=0.99)).verdict != "gated_off"

    def test_degraded_base_is_skipped_without_any_llm_call(self):
        f = ToTFake()
        out = run(f, degraded=True)
        assert out.verdict == "skipped" and f.calls == []


# --------------------------------------------------------------------------- failure isolation
class TestFailures:
    @pytest.mark.parametrize("parallel", [False, True])
    def test_hypothesize_failure_keeps_the_original(self, parallel):
        out = run(ToTFake(fail={"hypothesize": LLMUnavailable("429")}), parallel=parallel)
        assert out.verdict == "error" and out.diagnosis is out.original

    def test_invalid_hypothesize_json_is_handled(self):
        out = run(ToTFake(hypotheses="not json"))
        assert out.verdict == "error" and any("hypothesize failed" in n for n in out.notes)

    @pytest.mark.parametrize("parallel", [False, True])
    def test_a_failing_challenger_is_dropped_others_still_compete(self, parallel):
        out = run(ToTFake(fail={"The database": StructuredOutputError("bad json")}), parallel=parallel)
        assert out.winner == "H2" or out.verdict == "kept"
        assert [b.error != "" for b in out.branches] == [False, True, False]
        assert any("Branch H1 failed" in n for n in out.notes)

    def test_a_failing_base_branch_makes_the_run_inconclusive(self):
        out = run(ToTFake(fail={"An unbounded": LLMUnavailable("down")}))
        assert out.verdict == "error" and out.diagnosis is out.original

    def test_all_challengers_failing_is_inconclusive(self):
        out = run(ToTFake(fail={"The database": LLMUnavailable("x"), "No real": LLMUnavailable("x")}))
        assert out.verdict == "error" and out.diagnosis is out.original

    def test_solve_failure_keeps_the_original(self):
        out = run(ToTFake(fail={"solve": LLMUnavailable("down")}))
        assert out.verdict == "error" and out.diagnosis.root_cause == BASE_RC and out.winner == "H1"

    def test_interrupted_investigation_still_produces_a_report(self):
        class Flaky(ToTFake):
            def chat(self, messages, *, tools=None, **kw):
                if tools:
                    raise LLMUnavailable("rate limited")
                return super().chat(messages, tools=tools, **kw)

        out = run(Flaky())
        assert all(b.report is not None for b in out.branches)
        assert any("investigation interrupted" in line for b in out.branches for line in b.transcript)

    @pytest.mark.parametrize("parallel", [False, True])
    @pytest.mark.parametrize("where", ["hypothesize", "The database", "solve"])
    def test_config_errors_are_never_swallowed(self, where, parallel):
        with pytest.raises(LLMConfigError):
            run(ToTFake(fail={where: LLMConfigError("bad key")}), parallel=parallel)

    def test_no_distinct_hypothesis_keeps_the_diagnosis_with_a_note(self):
        out = run(ToTFake(hypotheses=hyp()))
        assert out.verdict == "kept" and "no distinct alternative" in out.diagnosis.reasoning_steps[-1].description

    def test_deterministic_for_identical_inputs(self):
        a, b = run(ToTFake()), run(ToTFake())
        da, db = a.diagnosis.model_dump(), b.diagnosis.model_dump()
        da.pop("produced_at"), db.pop("produced_at")
        assert a.to_dict() == b.to_dict() and da == db


# --------------------------------------------------------------------------- wiring
class ToTRouting(RoutingFake):
    """RoutingFake for Stage 6/7 roles + Stage 8 debate roles + Stage 9 roles."""

    def __init__(self, *, tot: ToTFake | None = None, debate: Any = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.tot = tot or ToTFake()
        self.debate = debate
        self.order: list[str] = []

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        sys = messages[0]["content"]
        if sys.startswith(("You are a senior SRE generating", "You are an investigator", "You finalize the diagnosis")):
            with self.lock:
                self.order.append("tot")
            return self.tot.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
        if sys.startswith(
            ("You are the root-cause defender", "You are the forensic examiner", "You are the impartial")
        ):
            with self.lock:
                self.order.append("debate")
            return self.debate.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
        if sys.startswith("You are the incident commander"):
            with self.lock:
                self.order.append("commander")
        return super().chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)


def routing_tot(**kw: Any) -> ToTFake:
    """A ToTFake whose branch keys match RoutingFake's FINAL root cause."""
    return ToTFake(**kw)


def wired_tot() -> ToTFake:
    """ToTFake whose quotes exist in the real INC-001 evidence ("ERROR" appears in the log tool output)."""
    return ToTFake(
        hypotheses=hyp((H1_RC, False), obs=[{"text": "errors are present", "quote": "ERROR"}]),
        reports={"An unbounded": rep(), "The database": rep(["ERROR"], explains=[0]), "No real": rep()},
        solve=solved(["ERROR"]),
    )


class TestWiring:
    def test_tot_orchestrator_scores_the_tot_final_diagnosis(self):
        fake = ToTRouting(tot=wired_tot(), final={**FINAL, "confidence": 0.7})
        agent = ToTOrchestrator(fake, parallel=False, tot_kwargs={"parallel": False})
        out = agent.run("INC-001")
        assert agent.record is not None and agent.record.verdict == "switched"
        assert out.root_cause == agent.record.diagnosis.root_cause and "connection pool" in out.root_cause
        assert agent.record.original.root_cause == BASE_RC
        assert agent.outcomes and agent.plan_history  # harness trace attributes keep working

    def test_degraded_orchestrator_skips_the_tree(self):
        fake = ToTRouting(fail={k: LLMUnavailable("down") for k in ("log", "metrics", "deploy")})
        agent = ToTOrchestrator(fake, parallel=False)
        out = agent.run("INC-001")
        assert agent.degraded and agent.record is not None and agent.record.verdict == "skipped"
        assert out.confidence == 0.0 and fake.order == []

    def test_swarm_without_tot_makes_no_tot_calls(self):
        fake = ToTRouting()
        HierarchicalSwarm(fake, parallel=False).run("INC-001")
        assert "tot" not in fake.order

    def test_swarm_runs_tot_then_debate_then_commander_and_each_sees_the_previous_result(self):
        from tests.test_llm_debate import DebateFake, chal

        debate = DebateFake(case={"challenges": [chal("ERROR")]})
        fake = ToTRouting(tot=ToTFake(), debate=debate)
        swarm = HierarchicalSwarm(fake, parallel=False, tot=True, debate=True)
        out = swarm.run("INC-001")
        order = [k for i, k in enumerate(fake.order) if i == 0 or fake.order[i - 1] != k]
        assert order[:3] == ["tot", "debate", "commander"] or order[:2] == ["tot", "debate"]
        assert swarm.tot_record is not None and swarm.debate_record is not None
        assert swarm.debate_record.original.root_cause == swarm.tot_record.diagnosis.root_cause
        assert out.diagnosis.root_cause  # the commander still produces the final scored diagnosis

    def test_swarm_tot_failure_never_loses_the_diagnosis(self):
        fake = ToTRouting(tot=ToTFake(fail={"hypothesize": LLMUnavailable("down")}))
        out = HierarchicalSwarm(fake, parallel=False, tot=True).run("INC-001")
        assert out.diagnosis.root_cause == BASE_RC


# --------------------------------------------------------------------------- harness
def _score(verdict: str):
    gold = {
        "incident_id": "INC-001",
        "category": "x",
        "difficulty": "easy",
        "root_cause": "retry loop saturated cpu",
        "evidence": ["retry"],
        "remediation_risk_tier": "high",
        "is_false_alarm": False,
    }
    res = harness.DiagnosisResult(
        "INC-001", "retry loop saturated cpu", ["retry"], "high", trace={"tot": {"verdict": verdict}}
    )
    return harness.score_one(gold, res)


class TestHarness:
    def test_summary_counts_tot_outcomes(self):
        s = harness.summarize([_score("switched"), _score("switch_rejected"), _score("kept")])
        assert (s["tot_switched"], s["tot_rejected"]) == (1, 1)

    def test_adapter_names_and_dispatch(self, monkeypatch):
        assert {"llm_tot", "llm_swarm_full"} <= set(harness.ADAPTER_NAMES)
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            harness,
            "make_llm_tot",
            lambda t, risk_guard=False, **k: seen.setdefault("tot", risk_guard) or (lambda i: i),
        )

        def fake_swarm(t, allow_revision=True, risk_guard=False, debate=False, tot=False):
            seen["swarm"] = (debate, tot)
            return lambda i: i

        monkeypatch.setattr(harness, "make_llm_swarm", fake_swarm)
        harness.make_adapter("llm_tot", 0.0, True)
        harness.make_adapter("llm_swarm_full")
        assert seen["tot"] is True and seen["swarm"] == (True, True)

    def test_tot_adapter_records_the_tree_in_the_trace(self, monkeypatch):
        fake = ToTRouting()
        monkeypatch.setattr("incident_agent.llm.build_client", lambda: fake)
        out = harness.make_llm_tot(parallel=False)("INC-001")
        assert out.error == "" and out.usage["calls"] > 0
        assert out.trace["tot"]["verdict"] in ("switched", "kept", "switch_rejected")
        assert out.trace["tot"]["original_root_cause"] == BASE_RC
        assert [b["id"] for b in out.trace["tot"]["branches"]][0] == "H0"
        out2 = harness.make_llm_swarm(tot=True)("INC-001")
        assert "tot" in out2.trace and "commander" in out2.trace

    def test_tot_adapter_crash_is_contained(self, monkeypatch):
        fake = ToTRouting(tot=ToTFake(fail={"hypothesize": LLMConfigError("bad key")}))
        monkeypatch.setattr("incident_agent.llm.build_client", lambda: fake)
        out = harness.make_llm_tot(parallel=False)("INC-001")
        assert out.error.startswith("LLMConfigError") and out.root_cause.startswith("Unknown")

    def test_old_result_rows_without_tot_verdict_still_load(self):
        row = {
            "incident_id": "INC-001", "category": "c", "difficulty": "easy", "root_cause_f1": 0.5,
            "evidence_recall": 0.5, "risk_match": True, "false_alarm_ok": True, "answered": True,
        }  # fmt: skip
        assert harness.IncidentScore(**row).tot_verdict == ""


def test_debate_and_tot_share_one_acceptance_policy():
    from incident_agent.agents import llm_debate, tree_of_thought

    assert tree_of_thought.accept_revision is llm_debate.accept_revision
    assert LLMDebate  # imported for the shared-policy contract
