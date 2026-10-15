"""Dataset v2 (INC-021..028): hard false alarms and false-negative traps for Stage 8.

These tests protect three things: v1 is untouched, the new gold labels are consistent with what the
agent-visible data really shows, and the splits stay balanced and leak-free.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from incident_agent.tools.toolbox import IncidentToolbox

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "gold"
DATA = ROOT / "data" / "incidents"
FALSE_ALARMS = ["INC-021", "INC-022", "INC-023", "INC-024"]
TRAPS = ["INC-025", "INC-026", "INC-027", "INC-028"]
NEW = FALSE_ALARMS + TRAPS


def gold(iid: str) -> dict:
    return json.loads((GOLD / f"{iid}.json").read_text())


def test_v1_incidents_and_gold_are_byte_identical_to_the_pinned_hashes():
    pinned = json.loads((Path(__file__).parent / "dataset_v1_hashes.json").read_text())
    assert len(pinned) == 20 * 5
    for key, digest in pinned.items():
        kind, iid, *rest = key.split("/")
        path = GOLD / f"{iid}.json" if kind == "gold" else DATA / iid / rest[0]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"{key} changed"


def test_labels_and_balance():
    assert [iid for iid in NEW if gold(iid)["is_false_alarm"]] == FALSE_ALARMS
    assert all(gold(i)["remediation_risk_tier"] == "low" for i in FALSE_ALARMS)
    assert not any(gold(i)["is_false_alarm"] for i in TRAPS)
    assert all("false-negative trap" in gold(i)["distractor_note"].lower() for i in TRAPS)
    assert all("hard false alarm" in gold(i)["distractor_note"].lower() for i in FALSE_ALARMS)


def test_splits_cover_everything_once_and_are_balanced():
    sp = json.loads((ROOT / "eval" / "splits.json").read_text())
    dev, test = sp["dev"], sp["test"]
    assert set(dev).isdisjoint(test) and len(dev) == len(set(dev)) == 14 and len(test) == len(set(test)) == 14
    assert set(dev) | set(test) == {f"INC-{i:03d}" for i in range(1, 29)}
    for group in (FALSE_ALARMS, TRAPS):
        assert sum(i in dev for i in group) == 2 and sum(i in test for i in group) == 2
    all_fa = [i for i in dev + test if gold(i)["is_false_alarm"]]
    assert len(all_fa) == 5  # INC-019 + the four new ones
    assert sum(gold(i)["is_false_alarm"] for i in test) == 2  # the test split can now measure false positives


def _series(iid: str, name: str) -> list[float]:
    return [p["value"] for p in json.loads((DATA / iid / "metrics.json").read_text())[name]]


@pytest.mark.parametrize(
    ("iid", "metric"),
    [
        ("INC-021", "cpu_percent"),
        ("INC-022", "memory_percent"),
        ("INC-024", "requests_per_sec"),
        ("INC-023", "latency_p99_ms"),
    ],
)
def test_false_alarm_excursions_are_visible_and_recover(iid, metric):
    v = _series(iid, metric)
    head = sum(v[: len(v) // 5]) / (len(v) // 5)
    assert max(v) > 1.5 * head  # a real-looking excursion exists, so the alert is plausible
    assert abs(v[-1] - head) < 0.25 * head  # but the signal is back to normal at the end of the window


@pytest.mark.parametrize(
    ("iid", "metric", "floor"),
    [
        ("INC-025", "memory_percent", 75),
        ("INC-026", "error_rate_percent", 10),
        ("INC-027", "error_rate_percent", 2),
        ("INC-028", "messages_consumed_per_min", None),
    ],
)
def test_trap_signals_do_not_recover(iid, metric, floor):
    v = _series(iid, metric)
    if floor is None:
        assert v[-1] == 0.0 and max(v[:40]) > 700
    else:
        assert v[-1] > floor


def test_every_false_alarm_has_a_documented_explanation_the_tools_can_reach():
    for iid in FALSE_ALARMS:
        tb = IncidentToolbox(iid)
        text = (tb.call("get_recent_deploys", {}) + (DATA / iid / "logs.log").read_text()).lower()
        assert re.search(r"planned|scheduled|approved|exporter", text), iid
        assert "level=error" not in text


def test_maintenance_change_records_are_visible_before_the_window():
    for iid in ("INC-021", "INC-023", "INC-026", "INC-027"):
        assert "no deploys" not in IncidentToolbox(iid).call("get_recent_deploys", {}), iid


def test_traps_carry_a_tempting_benign_explanation_next_to_the_real_fault():
    assert "maintenance" in gold("INC-026")["root_cause"].lower()
    assert "batch" in gold("INC-028")["root_cause"].lower()
    log28 = (DATA / "INC-028" / "logs.log").read_text()
    assert "nightly digest job enqueued" in log28 and "consumer thread died" in log28
    assert "level=ERROR" not in (DATA / "INC-025" / "logs.log").read_text()  # the quiet one


def test_new_incidents_do_not_leak_the_answer_into_meta():
    for iid in NEW:
        meta = (DATA / iid / "meta.json").read_text().lower()
        assert (
            "root_cause" not in meta
            and "false alarm" not in meta
            and gold(iid)["category"].replace("_", " ") not in meta
        )


def test_generator_is_deterministic_for_the_new_incidents():
    before = {i: (DATA / i / "logs.log").read_bytes() for i in NEW}
    import subprocess
    import sys

    subprocess.run([sys.executable, str(ROOT / "data" / "generate_dataset.py")], check=True, capture_output=True)
    assert before == {i: (DATA / i / "logs.log").read_bytes() for i in NEW}
