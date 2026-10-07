from incident_agent.eval.harness import DiagnosisResult, evaluate, f1_overlap, react_baseline


def test_f1_identical_is_one():
    assert f1_overlap("unbounded retry loop", "unbounded retry loop") == 1.0


def test_f1_disjoint_is_zero():
    assert f1_overlap("disk full", "tls certificate expired") == 0.0


def test_perfect_oracle_scores_high():
    import json

    from incident_agent.eval.harness import GOLD_DIR

    def oracle(iid: str) -> DiagnosisResult:
        g = json.loads((GOLD_DIR / f"{iid}.json").read_text())
        rc = "false alarm, no incident" if g["is_false_alarm"] else g["root_cause"]
        return DiagnosisResult(iid, rc, g["evidence"], g["remediation_risk_tier"])

    s = evaluate(oracle)["summary"]
    assert s["risk_accuracy"] == 1.0 and s["evidence_recall"] == 1.0 and s["answered_rate"] == 1.0


def test_legacy_react_baseline_is_weak():
    """Documents the honest baseline the LLM agent must beat."""
    s = evaluate(react_baseline)["summary"]
    assert s["answered_rate"] <= 0.3


def test_crashes_are_reported_not_hidden():
    def boom(iid: str) -> DiagnosisResult:
        raise RuntimeError("provider exploded")

    rep = evaluate(boom, limit=2)
    assert rep["summary"]["crashed"] == 2
    assert "provider exploded" in rep["incidents"][0]["error"]
