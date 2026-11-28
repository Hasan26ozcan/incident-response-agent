# ruff: noqa: E501
"""Run a lab scenario with real processes and capture the result as an incident folder (``data/lab/<id>/`` + gold).

``run_local``: starts payments-stub and checkout-api as subprocesses on loopback ports, drives load, injects the fault,
polls ``/metrics`` every second, collects both services' logs, and writes meta/logs/metrics/deploys.
The same ``capture`` works against the Docker Compose lab (``--url``) because it only needs HTTP + the log directory.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from incident_agent.lab.app import iso
from incident_agent.lab.scenarios import SCENARIOS, Scenario

METRICS = ("cpu_percent", "latency_p99_ms", "mem_mb", "error_rate_percent", "rps")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LAB_DATA = PROJECT_ROOT / "data" / "lab"


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 3.0) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310  # nosec B310 - loopback lab URL
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,  # noqa: S310
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310  # nosec B310 - loopback lab URL
        return dict(json.loads(r.read() or b"{}"))


def wait_up(url: str, seconds: float = 15.0) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            http("GET", url + "/health")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"service at {url} did not start")


class LoadGen:
    def __init__(self, url: str, rps: float = 8.0) -> None:
        self.url, self.rps, self.stop = url, rps, threading.Event()
        self.mult = 1.0
        self.threads: list[threading.Thread] = []

    def _work(self) -> None:
        while not self.stop.is_set():
            with contextlib.suppress(
                urllib.error.URLError, OSError
            ):  # error statuses surface in the service's own metrics
                http("GET", self.url + "/checkout", timeout=4.0)
            self.stop.wait(1.0 / max(self.rps / 4 * self.mult, 0.1))

    def start(self, workers: int = 4) -> None:
        for _ in range(workers):
            t = threading.Thread(target=self._work, daemon=True)
            t.start()
            self.threads.append(t)

    def close(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)


class Capture:
    def __init__(self, url: str) -> None:
        self.url = url
        self.series: dict[str, list[dict[str, Any]]] = {m: [] for m in METRICS}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def _poll(self) -> None:
        while not self.stop.wait(1.0):
            try:
                snap = http("GET", self.url + "/metrics")
            except (urllib.error.URLError, OSError):
                continue
            ts = iso()
            for m in METRICS:
                if m in snap:
                    self.series[m].append({"ts": ts, "value": snap[m]})

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> dict[str, list[dict[str, Any]]]:
        self.stop.set()
        self.thread.join(timeout=5)
        return self.series


def merge_logs(log_dir: Path, services: list[str]) -> list[str]:
    lines: list[str] = []
    for s in services:
        p = log_dir / f"{s}.log"
        if p.exists():
            lines += [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return sorted(lines)  # lines start with an ISO timestamp, so a text sort is chronological


def write_incident(
    out: Path,
    scn: Scenario,
    *,
    start: str,
    end: str,
    services: list[str],
    logs: list[str],
    series: dict[str, list[dict[str, Any]]],
    deploys: list[dict[str, Any]],
    gold_dir: Path | None = None,
) -> Path:
    d = out / scn.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs.log").write_text("\n".join(logs) + "\n", encoding="utf-8")
    present = [m for m in METRICS if series.get(m)]
    (d / "metrics.json").write_text(json.dumps({m: series[m] for m in present}, indent=1), encoding="utf-8")
    (d / "deploys.json").write_text(json.dumps(deploys, indent=2), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(
            {
                "incident_id": scn.id,
                "alert_name": "Alert: elevated errors or latency — checkout-api",
                "services": services,
                "window_start": start,
                "window_end": end,
                "available_metrics": present,
                "note": "Captured from real processes by the fault lab. The metadata names services and window, never the cause.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    gdir = gold_dir or out / "gold"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / f"{scn.id}.json").write_text(json.dumps(scn.gold(), indent=2), encoding="utf-8")
    return d


def timeline(
    scn: Scenario,
    curl: str,
    purl: str,
    log_dir: Path,
    out: Path,
    baseline_s: float,
    fault_s: float,
    load: LoadGen,
    *,
    own_load: bool,
) -> Path:
    """Baseline -> inject -> hold -> capture. Works on loopback processes and on the Docker Compose lab alike.
    ``own_load``: the load generator is ours (local mode); otherwise a surge uses an extra generator on top of the lab's own."""
    cap = Capture(curl)
    start = iso()
    cap.start()
    time.sleep(baseline_s)
    extra: LoadGen | None = None
    if scn.surge > 1.0:
        if own_load:
            load.mult = scn.surge
        else:
            extra = LoadGen(curl, rps=8.0 * (scn.surge - 1.0))
            extra.start()
    if scn.deploy:
        http(
            "POST",
            curl + "/admin/deploy",
            {"version": scn.deploy[0], "description": scn.deploy[1], "broken": scn.broken_deploy},
        )
    elif scn.fault:
        http("POST", (purl if scn.target == "payments-stub" else curl) + "/admin/fault", {"name": scn.fault})
    time.sleep(fault_s)
    if extra:
        extra.close()
    series = cap.finish()
    end = iso()
    deploys = [
        {
            "deploy_id": "deploy-000",
            "timestamp": start,
            "service": "checkout-api",
            "version": "2.3.0",
            "description": "Routine release, no known issues",
        }
    ]
    with contextlib.suppress(urllib.error.URLError, OSError):
        for i, dep in enumerate(http("GET", curl + "/deploys").get("deploys", []), start=1):
            deploys.append({**dep, "deploy_id": f"deploy-{i:03d}"})
    return write_incident(
        out,
        scn,
        start=start,
        end=end,
        services=["checkout-api", "payments-stub"],
        logs=merge_logs(log_dir, ["checkout-api", "payments-stub"]),
        series=series,
        deploys=deploys,
    )


def reset_faults(curl: str, purl: str) -> None:
    for url in (curl, purl):
        with contextlib.suppress(urllib.error.URLError, OSError):
            http("POST", url + "/admin/fault", {"name": None})


def run_local(scenario_id: str, *, out: Path = LAB_DATA, baseline_s: float = 20.0, fault_s: float = 40.0) -> Path:
    scn = SCENARIOS[scenario_id]
    work = out / ".work" / scn.id
    if work.exists():
        for f in work.glob("*.log"):
            f.unlink()
    work.mkdir(parents=True, exist_ok=True)
    pp, cp = free_port(), free_port()
    purl, curl = f"http://127.0.0.1:{pp}", f"http://127.0.0.1:{cp}"
    base = [sys.executable, "-m", "incident_agent.lab.app", "--host", "127.0.0.1", "--log-dir", str(work)]
    procs = [
        subprocess.Popen([*base, "--service", "payments-stub", "--port", str(pp)], stdout=subprocess.DEVNULL),  # noqa: S603  # nosec B603
        subprocess.Popen(  # noqa: S603  # nosec B603 - fixed argument list, no shell
            [*base, "--service", "checkout-api", "--port", str(cp), "--dep", purl], stdout=subprocess.DEVNULL
        ),
    ]
    load = LoadGen(curl)
    try:
        wait_up(purl)
        wait_up(curl)
        load.start()
        return timeline(scn, curl, purl, work, out, baseline_s, fault_s, load, own_load=True)
    finally:
        load.close()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
