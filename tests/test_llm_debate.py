"""Stage 8: LLM debate. Roadmap: root-cause agent vs forensic examiner, before/after false-positive comparison.

Conformance (two opposed roles, examiner has tools, arbiter rules), evidence gating, asymmetric acceptance
policy, calibrated confidence, failure isolation, wiring (orchestrator / swarm / harness / CLI).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from incident_agent.agents.grounding import ground_diagnosis
from incident_agent.agents.llm_commander import Signals
from incident_agent.agents.llm_debate import (
    MAX_STANDING_PENALTY,
    ArbiterRuling,
    DebatedOrchestrator,
    DebateResult,
    DefenceCase,
    ForensicCase,
    LLMDebate,
    Rebuttal,
)
from incident_agent.agents.llm_react_agent import LLMDiagnosis
from incident_agent.agents.swarm import HierarchicalSwarm
from incident_agent.eval import harness
from incident_agent.llm import LLMConfigError, LLMResponse, LLMUnavailable, StructuredOutputError, ToolCall
from incident_agent.schemas.agent_output import RiskTier
from incident_agent.tools.toolbox import TOOL_NAMES
from tests.fakes import FINAL, RoutingFake

EVIDENCE = (
    "ERROR checkout-api retry loop saturated CPU at 00:40 after deploy v2.3.1 raised the pool size. "
    "db latency normal. cache hit ratio healthy. planned maintenance window closed at 00:30."
)
GOOD_Q = "db latency normal"
GOOD_Q2 = "cache hit ratio healthy"
BAD_Q = "kernel panic node-7 caused unexpected reboot"
H, M, L = RiskTier.HIGH, RiskTier.MEDIUM, RiskTier.LOW


def diag(root: str | None = None, conf: float = 0.8, risk: str = "high", rec: str | None = None, fa: bool = False):
    data: dict[str, Any] = {**FINAL, "confidence": conf, "risk_tier": risk, "is_false_alarm": fa}
    if root:
        data["root_cause"] = root
    if rec:
        data["recommendation"] = rec
    return ground_diagnosis("t", "INC-001", LLMDiagnosis.model_validate(data), "retry loop")


def chal(quote: str = GOOD_Q, kind: str = "alternative_cause", sev: str = "medium") -> dict[str, Any]:
    return {"kind": kind, "point": "The database may be the real cause.", "quote": quote, "severity": sev}


def revised(quote_list: list[str], fa: bool = False, risk: str = "low", rec: str = "Monitor the service only."):
    return {
        "is_false_alarm": fa,
        "root_cause": "A connection pool misconfiguration after deploy v2.3.1 starved requests."
        if not fa
        else "Healthy: no real incident, metrics are normal.",
        "category": "db_connection",
        "recommendation": rec,
        "risk_tier": risk,
        "confidence": 0.85,
        "supporting_quotes": quote_list,
    }


class DebateFake:
    """Schema-driven fake: one scripted answer per debate role, with call log and fault injection."""

    model = "debate-fake"

    def __init__(
        self,
        *,
        defence: Any = None,
        case: Any = None,
        rebuttal: Any = None,
        ruling: Any = None,
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.defence = defence or {"claims": [{"claim": "retries saturate CPU", "quote": "retry loop saturated CPU"}]}
        self.case = case if case is not None else {"challenges": [chal()], "summary": "db?"}
        self.rebuttal = rebuttal if rebuttal is not None else {"responses": []}
        self.ruling = ruling or {"verdict": "confirmed", "reasoning": "The evidence supports the diagnosis."}
        self.fail = fail or {}
        self.calls: list[tuple[str, bool]] = []  # (role, request carried tools)
        self.user_prompts: dict[str, str] = {}
        self.tool_turns = 0

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        if tools:
            role = "examiner_tools"
        elif response_schema is DefenceCase:
            role = "defender"
        elif response_schema is ForensicCase:
            role = "examiner_case"
        elif response_schema is Rebuttal:
            role = "rebuttal"
        elif response_schema is ArbiterRuling:
            role = "arbiter"
        else:
            raise AssertionError(f"unexpected call: schema={response_schema}")
        self.calls.append((role, bool(tools)))
        self.user_prompts[role] = messages[-1]["content"]
        if role in self.fail:
            raise self.fail[role]
        if role == "examiner_tools":
            self.tool_turns += 1
            if self.tool_turns == 1:
                return LLMResponse(tool_calls=[ToolCall(id="t0", name="log_overview", arguments={})])
            return LLMResponse(content="done")
        body = {
            "defender": self.defence,
            "examiner_case": self.case,
            "rebuttal": self.rebuttal,
            "arbiter": self.ruling,
        }[role]
        return LLMResponse(content=body if isinstance(body, str) else json.dumps(body))

    @property
    def roles(self) -> list[str]:
        return [r for r, _ in self.calls]


def run(fake: DebateFake, d=None, *, signals=None, degraded=False, risk_guard=False, evidence: str = EVIDENCE):
    return LLMDebate(fake, risk_guard=risk_guard).run(
        "INC-001", "meta", d or diag(), evidence, signals=signals, degraded=degraded
    )


def sig(*, normal: bool) -> Signals:
    return Signals(
        3, 3, [], ["log", "metrics", "deploy"] if normal else [], [] if normal else ["log"], 1.0, None, normal
    )


# --------------------------------------------------------------------------- roadmap conformance
class TestConformance:
    def test_two_opposed_roles_then_an_arbiter_in_order(self):
        f = DebateFake(rebuttal={"responses": [{"challenge": 0, "answer": "normal latency", "quote": GOOD_Q}]})
        res = run(f)
        # defender -> examiner investigation (tools) -> examiner case -> rebuttal -> arbiter
        assert f.roles[0] == "defender"
        assert f.roles[1:-3] == ["examiner_tools"] * (len(f.roles) - 4)
        assert f.roles[-3:] == ["examiner_case", "rebuttal", "arbiter"]
        assert res.defence is not None and res.ruling is not None and res.rebuttal is not None

    def test_only_the_examiner_uses_tools_and_gets_every_read_only_tool(self):
        seen: list[set[str]] = []

        class Spy(DebateFake):
            def chat(self, messages, *, tools=None, **kw):
                if tools:
                    seen.append({t["function"]["name"] for t in tools})
                return super().chat(messages, tools=tools, **kw)

        f = Spy()
        run(f)
        assert seen and all(s == set(TOOL_NAMES) for s in seen)
        assert [role for role, had in f.calls if had and role != "examiner_tools"] == []

    def test_examiner_finds_evidence_the_defender_never_saw(self):
        # "ERROR" appears in the real tool output (log_overview on INC-001), not in the shared evidence text
        out = run(DebateFake(case={"challenges": [chal(quote="ERROR")]}), evidence="nothing relevant here at all")
        assert len(out.challenges) == 1 and out.discarded_challenges == 0
        assert any("log_overview" in line for line in out.examiner_transcript)

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_examiner_can_actually_call_every_tool(self, tool):
        class Caller(DebateFake):
            def chat(self, messages, *, tools=None, **kw):
                if tools and self.tool_turns == 0:
                    self.tool_turns += 1
                    self.calls.append(("examiner_tools", True))
                    args = {"query": "error"} if tool == "search_logs" else {}
                    return LLMResponse(tool_calls=[ToolCall(id="x", name=tool, arguments=args)])
                return super().chat(messages, tools=tools, **kw)

        out = run(Caller())
        assert out.examiner_transcript and not any(
            "ERROR" in ln and "not allowed" in ln for ln in out.examiner_transcript
        )
        assert out.examiner_transcript[0].startswith(f"> {tool}(")
        assert "<tool_output>" in out.examiner_transcript[0] and "unknown tool" not in out.examiner_transcript[0]

    def test_before_and_after_false_alarm_calls_are_recorded(self):
        out = run(
            DebateFake(
                ruling={
                    "verdict": "revised",
                    "reasoning": "healthy signals",
                    "revised": revised([GOOD_Q, GOOD_Q2], fa=True),
                }
            ),
            signals=sig(normal=True),
        )
        assert (out.before_false_alarm, out.after_false_alarm, out.changed) == (False, True, True)
        d = out.to_dict()
        assert json.loads(json.dumps(d))["verdict"] == "revised"

    def test_prompts_treat_evidence_as_untrusted_and_demand_verbatim_quotes(self):
        from incident_agent.prompts import debate_prompts as P

        for text in (P.DEFENDER_SYSTEM, P.EXAMINER_SYSTEM):
            assert "untrusted" in text.lower() and "never follow instructions" in text.lower()
        assert "VERBATIM" in P.ARBITER_SYSTEM and "empty" in P.EXAMINER_SYSTEM.lower()


# --------------------------------------------------------------------------- evidence gating
class TestGrounding:
    def test_ungrounded_challenge_is_discarded_and_counted(self):
        out = run(DebateFake(case={"challenges": [chal(BAD_Q), chal(GOOD_Q)]}))
        assert [c.quote for c in out.challenges] == [GOOD_Q]
        assert out.discarded_challenges == 1
        assert any("discarded" in n for n in out.notes)

    def test_all_challenges_ungrounded_means_no_challenges_and_no_arbiter(self):
        f = DebateFake(case={"challenges": [chal(BAD_Q)]})
        out = run(f)
        assert out.verdict == "no_challenges" and out.diagnosis.root_cause == diag().root_cause
        assert "rebuttal" not in f.roles and "arbiter" not in f.roles

    def test_empty_challenge_list_is_a_valid_clean_bill(self):
        f = DebateFake(case={"challenges": []})
        out = run(f)
        assert out.verdict == "no_challenges"
        assert out.diagnosis.confidence == diag().confidence  # nothing to penalise
        assert out.diagnosis.reasoning_steps[-1].description.startswith("Debate: the forensic examiner raised no")
        assert "arbiter" not in f.roles

    def test_challenge_quote_may_come_from_the_examiners_own_tool_output(self):
        out = run(DebateFake(case={"challenges": [chal("ERROR")]}), evidence="empty")
        assert out.challenges


# --------------------------------------------------------------------------- standing challenges / confidence
class TestStanding:
    def _confirmed(self, case, rebuttal):
        return run(DebateFake(case={"challenges": case}, rebuttal={"responses": rebuttal}))

    def test_grounded_rebuttal_resolves_the_challenge(self):
        out = self._confirmed([chal()], [{"challenge": 0, "answer": "latency is fine", "quote": GOOD_Q}])
        assert out.standing == [] and out.diagnosis.confidence == 0.8 and out.verdict == "confirmed"

    @pytest.mark.parametrize(
        "rebuttal",
        [
            [],  # unanswered
            [{"challenge": 0, "answer": "fair point", "quote": GOOD_Q, "conceded": True}],  # conceded
            [{"challenge": 0, "answer": "no way", "quote": ""}],  # no quote
            [{"challenge": 0, "answer": "no way", "quote": BAD_Q}],  # ungrounded quote
            [{"challenge": 7, "answer": "wrong index", "quote": GOOD_Q}],  # answers a challenge that does not exist
        ],
    )
    def test_unanswered_conceded_or_unquoted_challenges_stand(self, rebuttal):
        out = self._confirmed([chal()], rebuttal)
        assert out.standing == [0]
        assert out.diagnosis.confidence == 0.7  # 0.8 - 0.1
        assert "1 standing" in out.diagnosis.reasoning_steps[-1].description

    def test_penalty_is_capped(self):
        out = self._confirmed([chal() for _ in range(4)], [])
        assert len(out.standing) == 4
        assert out.diagnosis.confidence == round(0.8 - MAX_STANDING_PENALTY, 2)

    def test_penalty_never_goes_below_zero(self):
        out = run(DebateFake(case={"challenges": [chal(), chal()]}), diag(conf=0.1))
        assert out.diagnosis.confidence == 0.0

    def test_mixed_resolution_counts_only_the_standing_ones(self):
        out = self._confirmed([chal(), chal(GOOD_Q2)], [{"challenge": 1, "answer": "cache is fine", "quote": GOOD_Q2}])
        assert out.standing == [0] and out.diagnosis.confidence == 0.7

    def test_arbiter_prompt_shows_standing_flags_computed_by_code(self):
        f = DebateFake()
        run(f)
        assert '"standing": true' in f.user_prompts["arbiter"]


# --------------------------------------------------------------------------- revision policy
class TestRevisionPolicy:
    def _rule(self, rev, *, d=None, signals=None, fa_start=False):
        f = DebateFake(ruling={"verdict": "revised", "reasoning": "contradicted by evidence", "revised": rev})
        return run(f, d or diag(fa=fa_start, root="False alarm: healthy." if fa_start else None), signals=signals)

    def test_grounded_revision_replaces_the_diagnosis(self):
        out = self._rule(revised([GOOD_Q, GOOD_Q2]))
        assert out.verdict == "revised" and out.changed
        assert "connection pool" in out.diagnosis.root_cause
        assert any(e.source == "debate:quote" and e.confidence_weight == 1.0 for e in out.diagnosis.evidence)
        assert len(out.diagnosis.evidence) == len(diag().evidence) + 2
        assert out.diagnosis.confidence == 0.85
        assert out.diagnosis.agent_type == "llm_debate"

    def test_half_grounded_revision_is_accepted_with_penalty(self):
        out = self._rule(revised([GOOD_Q, BAD_Q]))
        assert out.verdict == "revised" and out.diagnosis.confidence == 0.7  # 0.85 - 0.15
        assert sorted(e.confidence_weight for e in out.diagnosis.evidence if e.source == "debate:quote") == [0.3, 1.0]

    def test_mostly_ungrounded_revision_is_rejected_and_original_kept(self):
        out = self._rule(revised([GOOD_Q, BAD_Q, BAD_Q + " again"]))
        assert out.verdict == "revision_rejected" and not out.changed
        assert out.diagnosis.root_cause == diag().root_cause
        assert any("rejected" in n for n in out.notes)
        assert out.diagnosis.confidence == 0.7  # one standing challenge still costs confidence

    def test_dismissing_a_real_incident_needs_two_quotes(self):
        out = self._rule(revised([GOOD_Q], fa=True), signals=sig(normal=True))
        assert out.verdict == "revision_rejected" and not out.after_false_alarm

    def test_dismissing_a_real_incident_needs_every_quote_grounded(self):
        out = self._rule(revised([GOOD_Q, GOOD_Q2, BAD_Q], fa=True), signals=sig(normal=True))
        assert out.verdict == "revision_rejected"

    def test_dismissing_a_real_incident_needs_every_domain_normal(self):
        out = self._rule(revised([GOOD_Q, GOOD_Q2], fa=True), signals=sig(normal=False))
        assert out.verdict == "revision_rejected" and not out.after_false_alarm

    def test_dismissal_accepted_when_two_grounded_quotes_and_all_domains_normal(self):
        out = self._rule(revised([GOOD_Q, GOOD_Q2], fa=True), signals=sig(normal=True))
        assert out.verdict == "revised" and out.after_false_alarm
        assert out.diagnosis.root_cause.lower().startswith("false alarm")

    def test_dismissal_without_worker_signals_still_needs_two_grounded_quotes(self):
        assert self._rule(revised([GOOD_Q, GOOD_Q2], fa=True)).verdict == "revised"
        assert self._rule(revised([GOOD_Q], fa=True)).verdict == "revision_rejected"

    def test_false_alarm_turned_real_uses_the_normal_half_rule(self):
        out = self._rule(revised([GOOD_Q]), fa_start=True)
        assert out.verdict == "revised" and out.before_false_alarm and not out.after_false_alarm

    def test_false_alarm_stays_false_alarm_is_not_a_dismissal(self):
        out = self._rule(revised([GOOD_Q], fa=True), fa_start=True)
        assert out.verdict == "revised"  # same call as before: ordinary half-grounded rule applies

    def test_revised_ruling_without_a_revised_diagnosis_is_treated_as_confirmed(self):
        f = DebateFake(ruling={"verdict": "revised", "reasoning": "I changed my mind but forgot to say how"})
        out = run(f)
        assert out.verdict == "confirmed" and any("without a revised diagnosis" in n for n in out.notes)

    def test_revision_may_not_lower_the_risk_tier_silently_but_guard_can_raise_it(self):
        rev = revised([GOOD_Q], risk="low", rec="Restart the service and roll back the pool change.")
        f = lambda: DebateFake(ruling={"verdict": "revised", "reasoning": "contradicted", "revised": rev})  # noqa: E731
        assert run(f()).diagnosis.risk_tier is L
        assert run(f(), risk_guard=True).diagnosis.risk_tier is H


# --------------------------------------------------------------------------- failure isolation
class TestFailures:
    def test_defender_failure_does_not_stop_the_debate(self):
        out = run(DebateFake(fail={"defender": LLMUnavailable("429")}))
        assert out.defence is None and out.verdict == "confirmed"
        assert any("defender failed" in n for n in out.notes)

    def test_examiner_case_failure_keeps_the_original(self):
        f = DebateFake(fail={"examiner_case": StructuredOutputError("bad json")})
        out = run(f)
        assert out.verdict == "error" and out.diagnosis.root_cause == diag().root_cause
        assert out.diagnosis.confidence == 0.8 and "arbiter" not in f.roles

    def test_examiner_investigation_interrupted_still_writes_a_case(self):
        class Flaky(DebateFake):
            def chat(self, messages, *, tools=None, **kw):
                if tools:
                    self.calls.append(("examiner_tools", True))
                    raise LLMUnavailable("rate limited")
                return super().chat(messages, tools=tools, **kw)

        out = run(Flaky())
        assert any("interrupted" in n for n in out.notes) and out.challenges

    def test_rebuttal_failure_leaves_every_challenge_standing(self):
        out = run(DebateFake(fail={"rebuttal": LLMUnavailable("down")}))
        assert out.standing == [0] and out.rebuttal is None and out.verdict == "confirmed"

    def test_arbiter_failure_keeps_the_original_unchanged(self):
        out = run(DebateFake(fail={"arbiter": LLMUnavailable("down")}))
        assert out.verdict == "error" and out.diagnosis is out.original and not out.changed

    @pytest.mark.parametrize("role", ["defender", "examiner_case", "rebuttal", "arbiter"])
    def test_config_errors_are_never_swallowed(self, role):
        with pytest.raises(LLMConfigError):
            run(DebateFake(fail={role: LLMConfigError("bad key")}))

    def test_config_error_during_investigation_propagates(self):
        class Bad(DebateFake):
            def chat(self, messages, *, tools=None, **kw):
                if tools:
                    raise LLMConfigError("model gone")
                return super().chat(messages, tools=tools, **kw)

        with pytest.raises(LLMConfigError):
            run(Bad())

    def test_degraded_base_is_skipped_without_any_llm_call(self):
        f = DebateFake()
        base = diag()
        out = LLMDebate(f).run("INC-001", "m", base, "x", degraded=True)
        assert out.verdict == "skipped" and out.diagnosis is base and f.calls == []

    def test_invalid_schema_answers_are_repaired_then_given_up_on(self):
        out = run(DebateFake(ruling="not json at all"))
        assert out.verdict == "error" and any("arbiter failed" in n for n in out.notes)

    def test_never_executes_remediation(self):
        # the examiner's toolbox only exposes read tools
        assert not any(t.startswith(("restart", "rollback", "scale", "delete")) for t in TOOL_NAMES)

    def test_deterministic_for_identical_inputs(self):
        a, b = run(DebateFake()), run(DebateFake())
        assert a.to_dict() == b.to_dict()
        da, db = a.diagnosis.model_dump(), b.diagnosis.model_dump()
        da.pop("produced_at"), db.pop("produced_at")  # wall-clock stamp, not part of the decision
        assert da == db


# --------------------------------------------------------------------------- wiring
class DebateRouting(RoutingFake):
    """RoutingFake for the Stage 6/7 roles plus the Stage 8 roles."""

    def __init__(self, *, debate: DebateFake | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.debate = debate or DebateFake()

    def chat(self, messages, *, tools=None, response_schema=None, temperature=0.0):
        sys = messages[0]["content"]
        if sys.startswith(
            ("You are the root-cause defender", "You are the forensic examiner", "You are the impartial")
        ):
            with self.lock:
                self.calls.append(("debate", response_schema.__name__ if response_schema else "tools"))
            return self.debate.chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)
        return super().chat(messages, tools=tools, response_schema=response_schema, temperature=temperature)


class TestWiring:
    def test_debated_orchestrator_scores_the_debates_final_diagnosis(self):
        rev = revised([GOOD_Q])
        # the quote must exist in the shared evidence: use one the worker report/tool output contains
        rev["supporting_quotes"] = ["ERROR"]
        fake = DebateRouting(
            debate=DebateFake(
                case={"challenges": [chal("ERROR")]},
                ruling={"verdict": "revised", "reasoning": "contradicted", "revised": rev},
            ),
            final={**FINAL, "confidence": 0.9},
        )
        agent = DebatedOrchestrator(fake, parallel=False)
        out = agent.run("INC-001")
        assert agent.record is not None and agent.record.verdict == "revised"
        assert out.root_cause == agent.record.diagnosis.root_cause and "connection pool" in out.root_cause
        assert agent.record.original.root_cause == FINAL["root_cause"]
        assert agent.outcomes and agent.plan_history  # harness trace attributes still work

    def test_degraded_orchestrator_skips_the_debate(self):
        fake = DebateRouting(
            fail={k: LLMUnavailable("down") for k in ("log", "metrics", "deploy")},
        )
        agent = DebatedOrchestrator(fake, parallel=False)
        out = agent.run("INC-001")
        assert agent.degraded and agent.record is not None and agent.record.verdict == "skipped"
        assert out.confidence == 0.0
        assert not [c for c in fake.calls if c[0] == "debate"]

    def test_swarm_without_debate_makes_no_debate_calls(self):
        fake = DebateRouting()
        HierarchicalSwarm(fake, parallel=False).run("INC-001")
        assert not [c for c in fake.calls if c[0] == "debate"]

    def test_swarm_with_debate_feeds_the_commander_the_debated_diagnosis(self):
        rev = {**revised(["ERROR"], rec="Restart nothing; observe only."), "confidence": 0.9}
        fake = DebateRouting(
            debate=DebateFake(
                case={"challenges": [chal("ERROR")]},
                ruling={"verdict": "revised", "reasoning": "contradicted", "revised": rev},
            ),
        )
        swarm = HierarchicalSwarm(fake, parallel=False, debate=True)
        out = swarm.run("INC-001")
        assert swarm.debate_record is not None and swarm.debate_record.verdict == "revised"
        assert "connection pool" in out.diagnosis.root_cause  # commander confirmed the debated diagnosis
        order = [c[0] for c in fake.calls]
        assert order.index("debate") < order.index("commander")

    def test_swarm_debate_failure_never_loses_the_diagnosis(self):
        fake = DebateRouting(debate=DebateFake(fail={"arbiter": LLMUnavailable("down")}))
        out = HierarchicalSwarm(fake, parallel=False, debate=True).run("INC-001")
        assert out.diagnosis.root_cause == FINAL["root_cause"]


# --------------------------------------------------------------------------- harness metrics and adapters
def _score(gold_fa: bool, claimed_fa: bool, verdict: str = ""):
    gold = {
        "incident_id": "INC-001",
        "category": "x",
        "difficulty": "easy",
        "root_cause": "retry loop saturated cpu",
        "evidence": ["retry"],
        "remediation_risk_tier": "high",
        "is_false_alarm": gold_fa,
    }
    root = "False alarm: nothing wrong" if claimed_fa else "retry loop saturated cpu"
    res = harness.DiagnosisResult(
        "INC-001", root, ["retry"], "high", trace={"debate": {"verdict": verdict}} if verdict else {}
    )
    return harness.score_one(gold, res)


class TestHarness:
    def test_false_positive_and_negative_rates(self):
        scores = [
            _score(True, False),  # FP: real claim on a false alarm
            _score(True, True),
            _score(False, True),  # FN: dismissed a real incident
            _score(False, False),
            _score(False, False),
        ]
        s = harness.summarize(scores)
        assert s["false_positive_rate"] == 0.5 and s["false_negative_rate"] == 0.333
        assert s["gold_false_alarms"] == 2

    def test_rates_are_none_when_undefined(self):
        s = harness.summarize([_score(False, False)])
        assert s["false_positive_rate"] is None and s["gold_false_alarms"] == 0
        assert harness.summarize([_score(True, True)])["false_negative_rate"] is None
        assert harness.summarize([])["false_positive_rate"] is None

    def test_debate_verdict_counts(self):
        s = harness.summarize(
            [_score(False, False, "revised"), _score(False, False, "revision_rejected"), _score(False, False)]
        )
        assert (s["debate_revised"], s["debate_rejected"]) == (1, 1)

    def test_old_result_rows_without_the_new_fields_still_load(self):
        row = {
            "incident_id": "INC-001", "category": "c", "difficulty": "easy", "root_cause_f1": 0.5,
            "evidence_recall": 0.5, "risk_match": True, "false_alarm_ok": True, "answered": True,
        }  # fmt: skip
        assert harness.IncidentScore(**row).gold_false_alarm is False

    def test_adapter_names_and_dispatch(self, monkeypatch):
        assert {"llm_debate", "llm_swarm_debate"} <= set(harness.ADAPTER_NAMES)
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            harness,
            "make_llm_debate",
            lambda t, risk_guard=False, **k: seen.setdefault("d", risk_guard) or (lambda i: i),
        )
        monkeypatch.setattr(
            harness,
            "make_llm_swarm",
            lambda t, allow_revision=True, risk_guard=False, debate=False: (
                seen.setdefault("s", debate) or (lambda i: i)
            ),
        )
        harness.make_adapter("llm_debate", 0.0, True)
        harness.make_adapter("llm_swarm_debate")
        assert seen["d"] is True and seen["s"] is True

    def test_debate_adapters_record_the_debate_in_the_trace(self, monkeypatch):
        fake = DebateRouting(debate=DebateFake(case={"challenges": [chal("ERROR")]}))
        monkeypatch.setattr("incident_agent.llm.build_client", lambda: fake)
        out = harness.make_llm_debate(parallel=False)("INC-001")
        assert out.trace["debate"]["verdict"] in ("confirmed", "revised", "revision_rejected")
        assert out.trace["debate"]["original_root_cause"] == FINAL["root_cause"]
        assert out.usage["calls"] > 0 and out.error == ""
        out2 = harness.make_llm_swarm(debate=True)("INC-001")
        assert "debate" in out2.trace and "commander" in out2.trace

    def test_debate_adapter_crash_is_contained(self, monkeypatch):
        fake = DebateRouting(
            debate=DebateFake(case={"challenges": [chal("ERROR")]}, fail={"arbiter": LLMConfigError("bad key")})
        )
        monkeypatch.setattr("incident_agent.llm.build_client", lambda: fake)
        out = harness.make_llm_debate(parallel=False)("INC-001")
        assert out.error.startswith("LLMConfigError") and out.root_cause.startswith("Unknown")


def test_debate_result_roundtrips_through_json_for_traces():
    res = run(DebateFake())
    assert isinstance(res, DebateResult)
    assert json.loads(json.dumps(res.to_dict()))["challenges"][0]["quote"] == GOOD_Q
