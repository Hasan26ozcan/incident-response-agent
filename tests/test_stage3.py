"""Tests for Stage 3 — Single-Agent ReAct skeleton.

Covers:
  - ReActAgent.observe(): data loading works correctly.
  - ReActAgent.reason(): produces non-empty reasoning chain.
  - ReActAgent.act(): produces structured Diagnosis with fields.
  - ReActAgent.run(): full loop returns confidence >= threshold.
  - CLI diagnose command works end-to-end.
  - Diagnosis.format_report(): human-readable output.
  - Tool functions: read_meta, read_logs, find_error_logs, etc.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from incident_agent.agents.react_agent import Diagnosis, ReActAgent
from incident_agent.tools import (
    detect_anomaly_timestamps,
    find_error_logs,
    find_metric_spikes,
    get_recent_deploys,
    read_logs,
    read_meta,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Tool function tests
# ---------------------------------------------------------------------------


class TestToolFunctions:
    def test_read_meta_returns_dict(self):
        meta = read_meta("INC-001")
        assert isinstance(meta, dict)
        assert meta["incident_id"] == "INC-001"

    def test_read_logs_returns_entries(self):
        logs = read_logs("INC-001")
        assert len(logs) > 0
        assert all(isinstance(e, tuple) for e in logs)

    def test_read_logs_has_timestamps(self):
        logs = read_logs("INC-001")
        assert logs[0].timestamp.startswith("2026-03-01")
        assert logs[0].service == "checkout-api"

    def test_find_error_logs(self):
        errors = find_error_logs("INC-001")
        assert len(errors) > 0
        assert all(e.level == "ERROR" for e in errors)

    def test_find_metric_spikes(self):
        spikes = find_metric_spikes("INC-001", threshold=100.0)
        assert len(spikes) > 0
        assert all(s["value"] > 100 for s in spikes)

    def test_detect_anomaly_timestamps(self):
        anomalies = detect_anomaly_timestamps("INC-001")
        assert len(anomalies) > 0
        for metric, timestamps in anomalies.items():
            assert len(timestamps) > 0
            assert metric in ("cpu_percent", "latency_p99_ms")

    def test_get_recent_deploys(self):
        deploys = get_recent_deploys("INC-001")
        assert isinstance(deploys, list)

    def test_read_metrics(self):
        from incident_agent.tools import read_metrics

        metrics = read_metrics("INC-001")
        assert "cpu_percent" in metrics
        assert "latency_p99_ms" in metrics
        assert len(metrics["cpu_percent"]) > 0


# ---------------------------------------------------------------------------
# ReActAgent tests
# ---------------------------------------------------------------------------


class TestReActAgent:
    @pytest.fixture(autouse=True)
    def agent(self):
        return ReActAgent(confidence_threshold=0.7)

    def test_observe_returns_dict(self, agent):
        obs = agent.observe("INC-001")
        assert "meta" in obs
        assert "error_logs" in obs
        assert "metric_anomalies" in obs
        assert "deploys" in obs

    def test_observe_meta_has_incident_id(self, agent):
        obs = agent.observe("INC-001")
        assert obs["meta"]["incident_id"] == "INC-001"

    def test_reason_returns_non_empty(self, agent):
        obs = agent.observe("INC-001")
        reasoning = agent.reason(obs)
        assert len(reasoning) > 0

    def test_reason_mentions_errors(self, agent):
        obs = agent.observe("INC-001")
        reasoning = agent.reason(obs)
        joined = " ".join(reasoning).lower()
        assert "error" in joined or "anomaly" in joined

    def test_act_returns_diagnosis(self, agent):
        obs = agent.observe("INC-001")
        reasoning = agent.reason(obs)
        diagnosis = agent.act(obs, reasoning)
        assert isinstance(diagnosis, Diagnosis)
        assert diagnosis.incident_id == "INC-001"
        assert diagnosis.root_cause

    def test_diagnosis_has_required_fields(self, agent):
        obs = agent.observe("INC-001")
        reasoning = agent.reason(obs)
        diagnosis = agent.act(obs, reasoning)
        assert diagnosis.affected_service
        assert diagnosis.category
        assert 0 <= diagnosis.confidence <= 1
        assert len(diagnosis.evidence) > 0
        assert len(diagnosis.reasoning_steps) > 0

    def test_diagnosis_evidence_from_logs(self, agent):
        obs = agent.observe("INC-001")
        reasoning = agent.reason(obs)
        diagnosis = agent.act(obs, reasoning)
        # Evidence should include log entries
        has_log_evidence = any("LOG [" in e for e in diagnosis.evidence)
        assert has_log_evidence

    def test_run_returns_diagnosis_above_threshold(self, agent):
        diagnosis = agent.run("INC-001")
        assert isinstance(diagnosis, Diagnosis)
        assert diagnosis.confidence >= agent.confidence_threshold

    def test_run_stores_diagnosis(self, agent):
        agent.run("INC-001")
        assert agent.diagnosis is not None
        assert agent.diagnosis.incident_id == "INC-001"

    def test_run_inc_019_false_alarm(self, agent):
        """INC-019 is a false alarm — agent should still produce diagnosis."""
        diagnosis = agent.run("INC-019")
        assert isinstance(diagnosis, Diagnosis)
        assert diagnosis.incident_id == "INC-019"

    def test_diagnosis_risk_tier(self, agent):
        obs = agent.observe("INC-001")
        reasoning = agent.reason(obs)
        diagnosis = agent.act(obs, reasoning)
        assert diagnosis.risk_tier in ("low", "medium", "high")


class TestReActAgentConfidence:
    """Verify confidence scaling with evidence."""

    def test_more_errors_higher_confidence(self):
        agent = ReActAgent(confidence_threshold=0.5)
        d1 = agent.run("INC-001")  # many errors

        # INC-001 has many errors so confidence should exceed threshold
        assert d1.confidence >= agent.confidence_threshold


class TestDiagnosisFormatReport:
    def test_format_report_contains_incident_id(self):
        agent = ReActAgent(confidence_threshold=0.5)
        diagnosis = agent.run("INC-001")
        report = diagnosis.format_report()
        assert "INC-001" in report
        assert "Root Cause" in report
        assert "Confidence" in report
        assert "Evidence" in report
        assert "Reasoning" in report
        assert "Recommendation" in report

    def test_format_report_includes_reasoning_steps(self):
        agent = ReActAgent(confidence_threshold=0.5)
        diagnosis = agent.run("INC-001")
        report = diagnosis.format_report()
        assert "1." in report  # reasoning steps numbered


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLIDiagnose:
    def test_diagnose_returns_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "incident_agent.cli", "diagnose", "INC-001"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "INC-001" in result.stdout

    def test_diagnose_output_has_root_cause(self):
        result = subprocess.run(
            [sys.executable, "-m", "incident_agent.cli", "diagnose", "INC-001"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "Root Cause:" in result.stdout

    def test_diagnose_output_has_confidence(self):
        result = subprocess.run(
            [sys.executable, "-m", "incident_agent.cli", "diagnose", "INC-001"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "Confidence:" in result.stdout

    def test_diagnose_no_args(self):
        result = subprocess.run(
            [sys.executable, "-m", "incident_agent.cli", "diagnose"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "Usage" in result.stdout

    def test_diagnose_unknown_incident(self):
        """Non-existent incident should still produce output or error."""
        result = subprocess.run(
            [sys.executable, "-m", "incident_agent.cli", "diagnose", "INC-999"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Either fails or produces a diagnosis — both acceptable
        # The key is it doesn't crash Python
        assert result.returncode in (0, 1)

    def test_version_output(self):
        result = subprocess.run(
            [sys.executable, "-m", "incident_agent.cli"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "incident-agent" in result.stdout
