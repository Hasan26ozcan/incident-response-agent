"""Stage 7 (hierarchical swarm): LLM Incident Commander over the orchestrator. Fully offline.

Organised by what ROADMAP.md promises for Stage 7:
  "Incident Commander agent added; synthesizes workers' findings. Output: end-to-end diagnosis flow (no actions yet)."
"""

from __future__ import annotations

import itertools

import pytest

from incident_agent.agents.llm_commander import (
    CommanderReview,
    LLMIncidentCommander,
    RevisedDiagnosis,
    Signals,
    claims_false_alarm,
    compute_signals,
    decide_escalation,
    fallback_severity,
    report_to_finding,
)
from incident_agent.agents.llm_orchestrator import LLMOrchestrator, WorkerOutcome, WorkerReport
from incident_agent.agents.swarm import HierarchicalSwarm
from incident_agent.llm import LLMConfigError, LLMUnavailable
from incident_agent.schemas import CommanderDiagnosis
from incident_agent.schemas.agent_output import RiskTier
from tests.fakes import FINAL, REPORT, REVIEW, RoutingFake

H, M, L = RiskTier.HIGH, RiskTier.MEDIUM, RiskTier.LOW


def _sig(
    ok=3,
    total=3,
    failed=(),
    normal=(),
    anomalous=("log", "metrics", "deploy"),
    ratio=1.0,
    spread=None,
    all_normal=False,
):
    return Signals(total, ok, list(failed), list(normal), list(anomalous), ratio, spread, all_normal)


def _outcome(name="log", *, report=None, transcript=None):
    return WorkerOutcome(
        name, report=report, transcript=transcript if transcript is not None else ["> log_overview({})\nERROR retry"]
    )


def _report(**over):
    return WorkerReport.model_validate({**REPORT, **over})


REVISION = {
    "is_false_alarm": False,
    "root_cause": "A config change at 00:40 exhausted the connection pool, not CPU.",
    "category": "db_connection",
    "recommendation": "Roll back the pool configuration change.",
    "risk_tier": "high",
    "confidence": 0.75,
    "supporting_quotes": ["ERROR"],
}


def _swarm(fake=None, **kw):
    fake = fake or RoutingFake()
    return fake, HierarchicalSwarm(fake, parallel=False, **kw)


# ======================================================================== roadmap conformance
class TestRoadmapConformance:
    def test_end_to_end_flow_returns_a_validated_commander_diagnosis(self):
        _, swarm = _swarm()
        out = swarm.run("INC-001")
        assert isinstance(out, CommanderDiagnosis)
        assert CommanderDiagnosis.from_json(out.to_json()).incident_id == "INC-001"
        assert out.agent_type == "llm_swarm"

    def test_commander_synthesises_every_workers_findings(self):
        _, swarm = _swarm()
        out = swarm.run("INC-001")
        assert {f.worker_type for f in out.worker_findings} == {"log", "metrics", "deploy"}
        assert all(f.incident_id == "INC-001" for f in out.worker_findings)
        assert out.narrative == REVIEW["narrative"]  # the commander's own synthesis is what is reported

    def test_hierarchy_order_triage_workers_synthesis_then_commander(self):
        fake, swarm = _swarm()
        swarm.run("INC-001")
        roles = [r for r, _ in fake.calls]
        assert roles[0] == "triage" and roles[-1] == "commander" and roles.index("synth") < roles.index("commander")
        assert roles.count("commander") == 1

    def test_no_actions_yet_commander_has_no_tools_and_remediation_is_not_executed(self):
        fake, swarm = _swarm()
        out = swarm.run("INC-001")
        assert [t for r, t in fake.tools_seen if r == "commander"] == [False]  # never offered a tool
        assert "not executed" in out.recommendation

    def test_diagnosis_flow_output_keeps_risk_tier_of_the_remediation(self):
        _, swarm = _swarm()
        assert swarm.run("INC-001").risk_tier is RiskTier.HIGH

    def test_eval_harness_can_run_the_swarm_end_to_end(self, monkeypatch):
        from incident_agent.eval import harness

        monkeypatch.setattr("incident_agent.llm.build_client", lambda *a, **k: RoutingFake())
        assert {"llm_swarm", "llm_swarm_norevise"} <= set(harness.ADAPTER_NAMES)
        run = harness.make_adapter("llm_swarm")
        res = run("INC-001")
        assert res.risk_tier == "high" and res.trace["commander"]["escalation"] == "escalate"
        assert "workers" in res.trace and res.usage["calls"] > 0 and not res.error
        assert harness.evaluate(run, ids=["INC-001"])["summary"]["crashed"] == 0

    def test_norevise_ablation_adapter_cannot_change_the_diagnosis(self, monkeypatch):
        from incident_agent.eval import harness

        fake = RoutingFake(review={**REVIEW, "agrees_with_diagnosis": False, "disagreement": "x", "revised": REVISION})
        monkeypatch.setattr("incident_agent.llm.build_client", lambda *a, **k: fake)
        res = harness.make_adapter("llm_swarm_norevise")("INC-001")
        assert res.trace["commander"]["revision_applied"] is False
        assert res.root_cause == FINAL["root_cause"]


# ======================================================================== escalation policy
class TestEscalationPolicy:
    def _d(self, **kw):
        base = dict(risk_tier=L, confidence=0.9, signals=_sig(), degraded=False, false_alarm=False)
        base.update(kw)
        return decide_escalation(**base)

    def test_each_rule_in_priority_order(self):
        assert self._d(degraded=True, risk_tier=L)[0] == "escalate"
        assert self._d(risk_tier=H, confidence=0.99)[0] == "escalate"
        assert self._d(risk_tier=M, confidence=0.5)[0] == "escalate"
        assert self._d(risk_tier=L, confidence=0.5)[0] == "escalate"
        assert self._d(risk_tier=M)[0] == "monitor"
        assert self._d(risk_tier=L, signals=_sig(ok=2, failed=["deploy"]))[0] == "monitor"
        assert self._d(risk_tier=L)[0] == "resolve"

    def test_a_false_alarm_that_still_carries_a_remediation_is_never_resolved(self):
        confirmed = _sig(normal=("log", "metrics", "deploy"), anomalous=(), all_normal=True)
        assert self._d(false_alarm=True, signals=confirmed, risk_tier=M)[0] == "monitor"

    def test_false_alarm_resolves_only_when_every_domain_confirms(self):
        confirmed = _sig(normal=("log", "metrics", "deploy"), anomalous=(), all_normal=True)
        assert self._d(false_alarm=True, signals=confirmed)[0] == "resolve"
        assert self._d(false_alarm=True, signals=_sig())[0] == "monitor"
        partial = _sig(ok=2, failed=["log"], normal=("metrics", "deploy"), anomalous=(), all_normal=True)
        assert self._d(false_alarm=True, signals=partial)[0] == "monitor"

    def test_every_reason_is_human_readable(self):
        for tier, conf, deg in itertools.product((H, M, L), (0.2, 0.9), (True, False)):
            decision, reason = self._d(risk_tier=tier, confidence=conf, degraded=deg)
            assert decision in {"escalate", "monitor", "resolve"} and len(reason) > 10

    @pytest.mark.parametrize("tier", [H, M, L])
    @pytest.mark.parametrize("conf", [0.0, 0.69, 0.7, 1.0])
    @pytest.mark.parametrize("degraded", [True, False])
    @pytest.mark.parametrize("false_alarm", [True, False])
    @pytest.mark.parametrize("ok", [0, 2, 3])
    def test_safety_invariants_hold_over_the_whole_input_space(self, tier, conf, degraded, false_alarm, ok):
        sig = _sig(ok=ok, failed=["x"] * (3 - ok), all_normal=ok > 0, anomalous=())
        decision, _ = self._d(risk_tier=tier, confidence=conf, degraded=degraded, false_alarm=false_alarm, signals=sig)
        if tier is H or degraded or conf < 0.7:
            assert decision == "escalate"  # a human is always involved when risk is high, run is degraded, or unsure
        if decision == "resolve":
            assert tier is L and ok == 3 and conf >= 0.7 and not degraded

    def test_threshold_is_configurable_and_inclusive(self):
        assert self._d(confidence=0.7, threshold=0.7)[0] == "resolve"
        assert self._d(confidence=0.7, threshold=0.71)[0] == "escalate"


# ======================================================================== signals
class TestSignals:
    def test_counts_and_domains(self):
        s = compute_signals(
            [
                _outcome("log", report=_report()),
                _outcome("metrics", report=_report(domain_normal=True)),
                _outcome("deploy"),
            ]
        )
        assert (s.workers_total, s.workers_ok, s.failed) == (3, 2, ["deploy"])
        assert s.normal_domains == ["metrics"] and s.anomalous_domains == ["log"] and not s.all_domains_normal

    def test_all_domains_normal(self):
        s = compute_signals([_outcome(n, report=_report(domain_normal=True)) for n in ("log", "metrics", "deploy")])
        assert s.all_domains_normal and s.anomalous_domains == []

    def test_no_reports_means_not_all_normal(self):
        assert compute_signals([_outcome("log"), _outcome("metrics")]).all_domains_normal is False

    def test_onset_spread_uses_only_anomalous_workers_with_parseable_times(self):
        s = compute_signals(
            [
                _outcome("log", report=_report(anomaly_onset="2026-03-01T00:40:20+00:00")),
                _outcome("metrics", report=_report(anomaly_onset="00:52")),
                _outcome("deploy", report=_report(domain_normal=True, anomaly_onset="09:00")),
            ]
        )
        assert s.onset_spread_minutes == 12 and not s.onsets_agree

    @pytest.mark.parametrize("onsets", [["00:40"], ["", "00:40"], ["unknown", "soon"]])
    def test_fewer_than_two_parseable_onsets_is_not_a_disagreement(self, onsets):
        outs = [
            _outcome(n, report=_report(anomaly_onset=o))
            for n, o in zip(("log", "metrics", "deploy"), onsets, strict=False)
        ]
        s = compute_signals(outs)
        assert s.onset_spread_minutes is None and s.onsets_agree

    def test_verified_ratio_counts_quotes_found_in_tool_output(self):
        facts = [
            {"claim": "real", "quote": "ERROR retry", "timestamp": ""},
            {"claim": "fake", "quote": "kernel panic zzqx node-9", "timestamp": ""},
        ]
        s = compute_signals([_outcome("log", report=_report(facts=facts))])
        assert s.verified_ratio == 0.5

    def test_verified_ratio_defaults_to_one_without_facts(self):
        assert compute_signals([_outcome("log", report=_report(facts=[]))]).verified_ratio == 1.0

    def test_describe_is_stable_text(self):
        assert "workers_ok=3/3" in _sig().describe()
        assert "onset_spread=n/a" in _sig().describe() and "12 min" in _sig(spread=12).describe()

    def test_false_alarm_detection(self):
        assert claims_false_alarm("  False alarm: nothing") and not claims_false_alarm("Real outage, not a false alarm")


# ======================================================================== review & revision
class TestReviewAndRevision:
    def _rev(self, **over):
        return {
            **REVIEW,
            "agrees_with_diagnosis": False,
            "disagreement": "Symptom was named as cause.",
            "revised": REVISION,
            **over,
        }

    def test_agreement_keeps_the_orchestrator_diagnosis(self):
        _, swarm = _swarm()
        out = swarm.run("INC-001")
        assert out.diagnosis.root_cause == FINAL["root_cause"] and swarm.result and not swarm.result.revision_applied

    def test_grounded_revision_is_applied_and_recorded(self):
        _, swarm = _swarm(RoutingFake(review=self._rev()))
        out = swarm.run("INC-001")
        assert swarm.result and swarm.result.revision_applied
        assert out.diagnosis.category == "db_connection" and "connection pool" in out.diagnosis.root_cause
        assert out.diagnosis.agent_type == "llm_swarm"
        assert any("Commander revised" in s.description for s in out.diagnosis.reasoning_steps)
        assert any(e.source == "commander:quote" and e.confidence_weight == 1.0 for e in out.diagnosis.evidence)
        assert len(out.diagnosis.evidence) > len(FINAL["evidence"])  # original evidence is kept

    def test_ungrounded_revision_is_rejected(self):
        bad = {**REVISION, "supporting_quotes": ["kernel panic on node-99 zzqx"]}
        _, swarm = _swarm(RoutingFake(review=self._rev(revised=bad)))
        out = swarm.run("INC-001")
        assert not swarm.result.revision_applied and out.diagnosis.root_cause == FINAL["root_cause"]
        assert any("rejected" in n for n in swarm.result.notes)

    def test_half_grounded_quotes_pass_the_threshold_and_the_ungrounded_one_costs_confidence(self):
        mixed = {**REVISION, "supporting_quotes": ["ERROR", "kernel panic zzqx node-99"], "confidence": 0.8}
        _, swarm = _swarm(RoutingFake(review=self._rev(revised=mixed)))
        out = swarm.run("INC-001")
        assert swarm.result.revision_applied and out.diagnosis.confidence == 0.65
        assert sorted(e.confidence_weight for e in out.diagnosis.evidence if e.source == "commander:quote") == [
            0.3,
            1.0,
        ]

    def test_revisions_can_be_disabled_for_ablation(self):
        _, swarm = _swarm(RoutingFake(review=self._rev()), allow_revision=False)
        out = swarm.run("INC-001")
        assert not swarm.result.revision_applied and out.diagnosis.root_cause == FINAL["root_cause"]
        assert any("revisions are disabled" in n for n in swarm.result.notes)

    def test_disagreement_without_a_revision_changes_nothing(self):
        _, swarm = _swarm(RoutingFake(review=self._rev(revised=None)))
        assert swarm.run("INC-001").diagnosis.root_cause == FINAL["root_cause"]

    def test_revision_to_false_alarm_is_prefixed_and_can_resolve(self):
        rev = {
            **REVISION,
            "is_false_alarm": True,
            "risk_tier": "low",
            "confidence": 0.9,
            "root_cause": "Nothing is wrong here.",
        }
        fake = RoutingFake(
            report={**REPORT, "domain_normal": True, "anomaly_onset": ""},
            review=self._rev(revised=rev, severity="SEV4"),
        )
        _, swarm = _swarm(fake)
        out = swarm.run("INC-001")
        assert out.diagnosis.root_cause.lower().startswith("false alarm") and out.risk_tier is RiskTier.LOW
        assert out.escalation_decision == "resolve"  # every domain normal, low risk, confident

    def test_revised_risk_tier_drives_escalation(self):
        low = {**REVISION, "risk_tier": "low", "confidence": 0.9}
        fake = RoutingFake(final={**FINAL, "risk_tier": "high"}, review=self._rev(revised=low))
        out = _swarm(fake)[1].run("INC-001")
        assert out.risk_tier is RiskTier.LOW and out.escalation_decision in {"resolve", "monitor"}

    def test_stale_time_in_narrative_costs_confidence_and_is_noted(self):
        stale = {**REVIEW, "narrative": "At 23:59 an unrelated outage began and checkout-api slowed down badly."}
        _, swarm = _swarm(RoutingFake(review=stale))
        out = swarm.run("INC-001")
        assert any("23:59" in n for n in swarm.result.notes)
        assert out.confidence == pytest.approx(FINAL["confidence"] - 0.05)

    def test_grounded_times_in_narrative_are_free(self):
        _, swarm = _swarm()
        out = swarm.run("INC-001")
        assert out.confidence == pytest.approx(FINAL["confidence"]) and not swarm.result.notes

    def test_severity_and_open_questions_reach_the_output(self):
        out = _swarm()[1].run("INC-001")
        assert out.severity_assessment.startswith("SEV2 - ") and "Open questions" in out.recommendation

    def test_invalid_severity_label_is_repaired_or_degrades_never_crashes(self):
        fake = RoutingFake(review={**REVIEW, "severity": "CRITICAL"})
        _, swarm = _swarm(fake)
        out = swarm.run("INC-001")
        assert swarm.result.fallback_reason.startswith("StructuredOutputError") and out.severity_assessment.startswith(
            "SEV2"
        )

    def test_review_schema_bounds(self):
        with pytest.raises(ValueError):
            CommanderReview.model_validate({**REVIEW, "narrative": "too short"})
        with pytest.raises(ValueError):
            RevisedDiagnosis.model_validate({**REVISION, "supporting_quotes": []})


# ======================================================================== failure handling
class TestFailureHandling:
    def test_commander_outage_falls_back_without_losing_the_diagnosis(self):
        _, swarm = _swarm(RoutingFake(fail={"commander": LLMUnavailable("down")}))
        out = swarm.run("INC-001")
        assert out.diagnosis.root_cause == FINAL["root_cause"] and swarm.result.review is None
        assert swarm.result.fallback_reason.startswith("LLMUnavailable")
        assert "log worker reports" in out.narrative and out.severity_assessment.startswith("SEV2 - derived from high")
        assert any("Commander model unavailable" in s.description for s in out.reasoning_steps)

    def test_garbage_commander_output_falls_back(self):
        _, swarm = _swarm(RoutingFake(review="not json"))
        out = swarm.run("INC-001")
        assert (
            swarm.result.fallback_reason.startswith("StructuredOutputError") and out.escalation_decision == "escalate"
        )

    def test_config_error_in_commander_is_not_swallowed(self):
        with pytest.raises(LLMConfigError):
            _swarm(RoutingFake(fail={"commander": LLMConfigError("bad key")}))[1].run("INC-001")

    def test_degraded_orchestrator_skips_the_commander_model_and_escalates(self):
        fake = RoutingFake()
        swarm = HierarchicalSwarm(fake, parallel=False, fail_workers={"log", "metrics", "deploy"})
        out = swarm.run("INC-001")
        assert swarm.degraded and "commander" not in [r for r, _ in fake.calls]
        assert out.escalation_decision == "escalate" and out.confidence == 0.0
        assert out.worker_findings[0].worker_type == "none"  # schema needs >= 1 finding

    def test_one_failed_worker_lowers_confidence_and_is_explained(self):
        swarm = HierarchicalSwarm(RoutingFake(), parallel=False, fail_workers={"metrics"})
        out = swarm.run("INC-001")
        assert out.confidence == pytest.approx(FINAL["confidence"] - 0.1)
        assert {f.worker_type for f in out.worker_findings} == {"log", "deploy"}
        assert any("metrics" in s.description and "failed" in s.description for s in out.reasoning_steps)

    def test_synthesis_failure_degrades_through_to_an_escalation(self):
        swarm = HierarchicalSwarm(RoutingFake(fail={"synth": LLMUnavailable("down")}), parallel=False)
        out = swarm.run("INC-001")
        assert swarm.degraded and out.escalation_decision == "escalate"

    def test_onset_disagreement_is_noted(self):
        class Skewed(RoutingFake):
            def chat(self, messages, **kw):
                role = self.role(messages)
                if kw.get("response_schema") is WorkerReport and role == "metrics":
                    self.report = {**REPORT, "anomaly_onset": "01:12"}
                return super().chat(messages, **kw)

        swarm = HierarchicalSwarm(Skewed(), parallel=False)
        out = swarm.run("INC-001")
        assert any("disagree on onset" in s.description for s in out.reasoning_steps)


# ======================================================================== conversion & fallbacks
class TestHelpers:
    def test_report_to_finding_maps_domains_and_weights_unverified_quotes(self):
        facts = [
            {"claim": "first claim", "quote": "ERROR retry", "timestamp": "00:40"},
            {"claim": "second claim", "quote": "zzqx node-99 panic", "timestamp": ""},
        ]
        f = report_to_finding(_outcome("deploy", report=_report(facts=facts)))
        assert f and [e.source_type.value for e in f.evidence] == ["deploy", "deploy"]
        assert [e.confidence_weight for e in f.evidence] == [1.0, 0.3]

    def test_report_to_finding_for_a_normal_domain_still_has_evidence(self):
        f = report_to_finding(_outcome("metrics", report=_report(facts=[], domain_normal=True)))
        assert f and f.evidence[0].source_type.value == "meta"

    def test_report_to_finding_is_none_for_failed_worker(self):
        assert report_to_finding(_outcome("log")) is None

    @pytest.mark.parametrize(("tier", "sev"), [(H, "SEV2"), (M, "SEV3"), (L, "SEV4")])
    def test_fallback_severity_table(self, tier, sev):
        assert fallback_severity(tier, False)[0] == sev

    def test_fallback_severity_for_false_alarm_is_sev4_regardless_of_tier(self):
        assert fallback_severity(H, True)[0] == "SEV4"


# ======================================================================== direct commander use
def test_commander_runs_standalone_on_orchestrator_output():
    fake = RoutingFake()
    orch = LLMOrchestrator(fake, parallel=False)
    diag = orch.run("INC-001")
    res = LLMIncidentCommander(fake).run("INC-001", "{}", diag, list(orch.outcomes.values()))
    assert res.output.escalation_decision == "escalate" and res.escalation_reason
    assert res.output.reasoning_steps[2].description.startswith("Escalation rule: escalate")
