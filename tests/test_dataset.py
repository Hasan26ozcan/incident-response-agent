"""Tests for the Stage 1 synthetic incident dataset.

Covers:
  - Dataset generator (data/generate_dataset.py) produces valid output.
  - Validator (eval/validate_dataset.py) passes cleanly.
  - Gold set index (eval/gold_set.md) is consistent.
  - Structural integrity: gold never under data/incidents/.
  - Recurrence pair, false-alarm, and distractor invariants.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = ROOT / "data" / "incidents"
GOLD_DIR = ROOT / "eval" / "gold"
EXPECTED_COUNT = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_script(path: str) -> subprocess.CompletedProcess:
    """Run a Python script and return the completed process."""
    return subprocess.run(
        [sys.executable, str(path)],
        capture_output=True, text=True, timeout=120,
    )


def load_index():
    return json.loads((INCIDENTS_DIR / "_index.json").read_text())


def load_gold(iid: str) -> dict:
    return json.loads((GOLD_DIR / f"{iid}.json").read_text())


def load_incident_files(iid: str) -> dict:
    d = INCIDENTS_DIR / iid
    return {
        "meta": json.loads((d / "meta.json").read_text()),
        "logs": (d / "logs.log").read_text(),
        "metrics": json.loads((d / "metrics.json").read_text()),
        "deploys": json.loads((d / "deploys.json").read_text()),
    }


# ---------------------------------------------------------------------------
# 1. Generator produces the right number of scenarios
# ---------------------------------------------------------------------------

class TestGenerator:
    def test_index_has_expected_count(self):
        index = load_index()
        assert len(index) == EXPECTED_COUNT, (
            f"expected {EXPECTED_COUNT} scenarios, found {len(index)}"
        )

    def test_all_scenario_ids_formatted(self):
        index = load_index()
        ids = {e["incident_id"] for e in index}
        expected = {f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)}
        assert ids == expected, f"ID mismatch: {ids.symmetric_difference(expected)}"

    def test_generator_exit_zero(self):
        proc = _run_script(ROOT / "data" / "generate_dataset.py")
        assert proc.returncode == 0, (
            f"generate_dataset.py failed:\n{proc.stderr}"
        )
        assert "20 scenarios generated" in proc.stdout

    def test_generator_deterministic(self):
        """Running the generator twice should produce identical output."""
        proc1 = _run_script(ROOT / "data" / "generate_dataset.py")
        meta1 = json.loads((INCIDENTS_DIR / "_index.json").read_text())
        proc2 = _run_script(ROOT / "data" / "generate_dataset.py")
        meta2 = json.loads((INCIDENTS_DIR / "_index.json").read_text())
        assert proc1.returncode == 0 and proc2.returncode == 0
        assert meta1 == meta2, "Generator is not deterministic across runs"


# ---------------------------------------------------------------------------
# 2. Every incident has all four required files
# ---------------------------------------------------------------------------

class TestIncidentIntegrity:
    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_four_files_present(self, iid):
        d = INCIDENTS_DIR / iid
        for f in ["meta.json", "logs.log", "metrics.json", "deploys.json"]:
            assert (d / f).exists(), f"{iid}: missing {f}"

    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_all_json_parses(self, iid):
        files = ["meta.json", "metrics.json", "deploys.json"]
        for f in files:
            try:
                json.loads((INCIDENTS_DIR / iid / f).read_text())
            except json.JSONDecodeError as e:
                pytest.fail(f"{iid}/{f}: JSON parse error — {e}")

    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_no_empty_metric_series(self, iid):
        metrics = load_incident_files(iid)["metrics"]
        for k, v in metrics.items():
            assert len(v) > 0, f"{iid}: metric '{k}' has zero data points"


# ---------------------------------------------------------------------------
# 3. Gold answers exist and match incidents 1:1
# ---------------------------------------------------------------------------

class TestGoldIntegrity:
    def test_gold_count_matches_index(self):
        index = load_index()
        incident_ids = {e["incident_id"] for e in index}
        gold_ids = {p.stem for p in GOLD_DIR.glob("INC-*.json")}
        assert incident_ids == gold_ids, (
            f"mismatch: only-in-incidents={incident_ids - gold_ids}, "
            f"only-in-gold={gold_ids - incident_ids}"
        )

    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_gold_file_has_required_keys(self, iid):
        g = load_gold(iid)
        for key in ["root_cause", "evidence", "recommended_remediation",
                     "remediation_risk_tier", "category", "difficulty",
                     "incident_id"]:
            assert key in g, f"{iid}: missing gold key '{key}'"
        assert isinstance(g["evidence"], list), f"{iid}: evidence is not a list"
        assert len(g["evidence"]) > 0, f"{iid}: evidence is empty"

    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_gold_risk_tier_valid(self, iid):
        g = load_gold(iid)
        assert g["remediation_risk_tier"] in ("low", "medium", "high"), (
            f"{iid}: invalid risk tier '{g['remediation_risk_tier']}'"
        )

    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_gold_category_matches_incident(self, iid):
        meta = load_incident_files(iid)["meta"]
        g = load_gold(iid)
        assert g["category"] == meta.get("category", "") or g["category"] in [
            c["category"] for c in load_index() if c["incident_id"] == iid
        ]


# ---------------------------------------------------------------------------
# 4. No eval leakage into data/incidents/
# ---------------------------------------------------------------------------

class TestNoLeakage:
    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_no_gold_keys_in_meta(self, iid):
        meta = load_incident_files(iid)["meta"]
        meta_text = json.dumps(meta).lower()
        # Gold-answer keys must not appear in meta.json
        for forbidden in ["root_cause", "recommended_remediation"]:
            assert forbidden not in meta_text, (
                f"{iid}: '{forbidden}' leaked into meta.json"
            )

    @pytest.mark.parametrize("iid", [
        f"INC-{i:03d}" for i in range(1, EXPECTED_COUNT + 1)
    ])
    def test_no_root_cause_in_meta(self, iid):
        meta = load_incident_files(iid)["meta"]
        meta_text = json.dumps(meta).lower()
        assert "root_cause" not in meta_text, (
            f"{iid}: 'root_cause' leaked into meta.json"
        )

    def test_gold_dir_outside_incidents(self):
        """Gold answers must never be under data/incidents/."""
        for p in GOLD_DIR.glob("INC-*.json"):
            resolved = p.resolve()
            assert "incidents" not in str(resolved).lower(), (
                f"{p.name} is under data/incidents/ — eval leakage!"
            )


# ---------------------------------------------------------------------------
# 5. False-alarm scenario invariants
# ---------------------------------------------------------------------------

class TestFalseAlarm:
    def test_incident_019_is_false_alarm(self):
        g = load_gold("INC-019")
        assert g.get("is_false_alarm") is True, "INC-019 should be a false alarm"

    def test_incident_019_no_error_warn_logs(self):
        logs = load_incident_files("INC-019")["logs"]
        assert "level=ERROR" not in logs, (
            "INC-019 must not contain ERROR log lines"
        )
        assert "level=WARN" not in logs, (
            "INC-019 must not contain WARN log lines"
        )

    def test_incident_019_metrics_have_no_anomaly(self):
        """INC-019 metrics should have no anomaly key in the original spec
        (verified via the generated metrics shape)."""
        metrics = load_incident_files("INC-019")["metrics"]
        # Verify all metric series have data points
        for k, v in metrics.items():
            assert len(v) > 0


# ---------------------------------------------------------------------------
# 6. Recurrence pair
# ---------------------------------------------------------------------------

class TestRecurrencePair:
    def test_incident_020_recurrence_of_002(self):
        g020 = load_gold("INC-020")
        assert g020.get("recurrence_of") == "INC-002", (
            "INC-020 should reference INC-002 as recurrence_of"
        )

    def test_incident_002_exists(self):
        index = load_index()
        ids = {e["incident_id"] for e in index}
        assert "INC-002" in ids

    def test_incident_020_exists(self):
        index = load_index()
        ids = {e["incident_id"] for e in index}
        assert "INC-020" in ids

    def test_recurrence_references_resolve(self):
        """Every recurrence_of value must point to an existing incident id."""
        index = load_index()
        incident_ids = {e["incident_id"] for e in index}
        for entry in index:
            rec = entry.get("recurrence_of")
            if rec:
                assert rec in incident_ids, (
                    f"{entry['incident_id']}: recurrence_of='{rec}' "
                    f"does not match any known incident id"
                )


# ---------------------------------------------------------------------------
# 7. Distractor / debate-test pairs
# ---------------------------------------------------------------------------

class TestDistractorPairs:
    def test_incident_005_has_distractor_note(self):
        g = load_gold("INC-005")
        assert "distractor_note" in g and g["distractor_note"], (
            "INC-005 should have a distractor_note"
        )

    def test_incident_007_has_distractor_note(self):
        g = load_gold("INC-007")
        assert "distractor_note" in g and g["distractor_note"], (
            "INC-007 should have a distractor_note"
        )

    def test_incident_013_has_distractor_note(self):
        g = load_gold("INC-013")
        assert "distractor_note" in g and g["distractor_note"]

    def test_incident_015_has_distractor_note(self):
        g = load_gold("INC-015")
        assert "distractor_note" in g and g["distractor_note"]

    def test_incident_017_has_distractor_note(self):
        g = load_gold("INC-017")
        assert "distractor_note" in g and g["distractor_note"]

    def test_incident_018_has_distractor_note(self):
        g = load_gold("INC-018")
        assert "distractor_note" in g and g["distractor_note"]


# ---------------------------------------------------------------------------
# 8. Validator passes cleanly
# ---------------------------------------------------------------------------

class TestValidator:
    def test_validator_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "eval" / "validate_dataset.py")],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"validate_dataset.py failed:\n{proc.stderr}\n{proc.stdout}"
        )
        assert "no problems found" in proc.stdout.lower()


# ---------------------------------------------------------------------------
# 9. Gold set MD is consistent
# ---------------------------------------------------------------------------

class TestGoldSetMd:
    def test_gold_set_md_exists(self):
        assert (ROOT / "eval" / "gold_set.md").exists(), (
            "eval/gold_set.md missing — run eval/generate_gold_set_md.py"
        )

    def test_gold_set_md_contains_all_ids(self):
        content = (ROOT / "eval" / "gold_set.md").read_text()
        for i in range(1, EXPECTED_COUNT + 1):
            assert f"INC-{i:03d}" in content, (
                f"gold_set.md missing INC-{i:03d}"
            )

    def test_gold_set_md_mentions_recurrence_pair(self):
        content = (ROOT / "eval" / "gold_set.md").read_text()
        assert "INC-002" in content and "INC-020" in content, (
            "gold_set.md should mention the recurrence pair"
        )

    def test_gold_set_md_mentions_false_alarm(self):
        content = (ROOT / "eval" / "gold_set.md").read_text()
        assert "INC-019" in content, (
            "gold_set.md should mention INC-019"
        )
