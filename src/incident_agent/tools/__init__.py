"""Tool functions for reading incident data.

Each function reads one data source from the synthetic dataset
and returns structured information the ReAct agent can reason over.
Stage 3: basic log/metrics/deploy reading.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class LogEntry(NamedTuple):
    timestamp: str
    service: str
    level: str
    message: str


def read_meta(incident_id: str) -> dict:
    """Read and return the meta.json for a given incident."""
    path = PROJECT_ROOT / "data" / "incidents" / incident_id / "meta.json"
    return json.loads(path.read_text())


def read_logs(incident_id: str) -> list[LogEntry]:
    """Parse logs.log into structured LogEntry objects."""
    path = PROJECT_ROOT / "data" / "incidents" / incident_id / "logs.log"
    entries: list[LogEntry] = []
    pattern = re.compile(
        r"(?P<ts>\S+)\s+service=(?P<service>\S+)\s+"
        r'level=(?P<level>\S+)\s+msg="(?P<msg>.*)"'
    )
    for line in path.read_text().splitlines():
        m = pattern.match(line.strip())
        if m:
            entries.append(
                LogEntry(
                    timestamp=m.group("ts"),
                    service=m.group("service"),
                    level=m.group("level"),
                    message=m.group("msg"),
                )
            )
    return entries


def read_metrics(incident_id: str) -> dict:
    """Read and return metrics.json for a given incident."""
    path = PROJECT_ROOT / "data" / "incidents" / incident_id / "metrics.json"
    return json.loads(path.read_text())


def read_deploys(incident_id: str) -> list[dict]:
    """Read and return deploys.json for a given incident."""
    path = PROJECT_ROOT / "data" / "incidents" / incident_id / "deploys.json"
    return json.loads(path.read_text())


def find_error_logs(incident_id: str) -> list[LogEntry]:
    """Return only ERROR-level log entries for an incident."""
    return [e for e in read_logs(incident_id) if e.level == "ERROR"]


def find_metric_spikes(incident_id: str, threshold: float = 300.0) -> list[dict]:
    """Return metric data points that exceed the threshold.

    Scans all metric series and returns points where value > threshold,
    useful for identifying anomalies like CPU spikes or latency jumps.
    """
    metrics = read_metrics(incident_id)
    spikes: list[dict] = []
    for metric_name, series in metrics.items():
        for point in series:
            if point["value"] > threshold:
                spikes.append(
                    {
                        "metric": metric_name,
                        "timestamp": point["ts"],
                        "value": point["value"],
                    }
                )
    return spikes


def detect_anomaly_timestamps(incident_id: str) -> dict[str, list[str]]:
    """Detect when anomalies start per metric.

    Returns a mapping of metric_name -> list of timestamps where
    the value first exceeds a significant threshold relative to
    the baseline (first 20% of data points).
    """
    metrics = read_metrics(incident_id)
    anomalies: dict[str, list[str]] = {}
    for metric_name, series in metrics.items():
        if not series:
            continue
        baseline = [p["value"] for p in series[: max(1, len(series) // 5)]]
        baseline_avg = sum(baseline) / len(baseline)
        baseline_stdev = (sum((v - baseline_avg) ** 2 for v in baseline) / len(baseline)) ** 0.5
        cutoff = baseline_avg + max(3 * baseline_stdev, baseline_avg * 2)
        anomaly_ts = [p["ts"] for p in series if p["value"] > cutoff]
        if anomaly_ts:
            anomalies[metric_name] = anomaly_ts[:3]  # first 3 anomaly points
    return anomalies


def get_recent_deploys(incident_id: str, window_hours: int = 48) -> list[dict]:
    """Return deploys that occurred within the given window before
    the incident window starts."""
    meta = read_meta(incident_id)
    deploys = read_deploys(incident_id)
    window_start = meta["window_start"]
    # Simple string comparison works for ISO-8601 timestamps
    recent = [d for d in deploys if d["timestamp"] < window_start]
    return recent
