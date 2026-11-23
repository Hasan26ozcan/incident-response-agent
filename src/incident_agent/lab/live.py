# ruff: noqa: E501
"""Drive the Docker Compose lab from the host: ``python -m incident_agent.lab.live capture LAB-003 --baseline 30 --fault 60``.

The compose stack runs checkout-api, payments-stub and a load generator and writes logs to ``lab/out/logs``.
Capture clears those logs, runs the scenario timeline over HTTP and writes the incident into ``data/lab/``.
``reset`` clears injected faults. Everything stays on 127.0.0.1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.lab import runner
from incident_agent.lab.scenarios import SCENARIOS

CURL, PURL = "http://127.0.0.1:8080", "http://127.0.0.1:8081"
LOG_DIR = runner.PROJECT_ROOT / "lab" / "out" / "logs"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incident_agent.lab.live")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("scenario", choices=sorted(SCENARIOS))
    c.add_argument("--baseline", type=float, default=30.0)
    c.add_argument("--fault", type=float, default=60.0)
    c.add_argument("--out", type=Path, default=runner.LAB_DATA)
    sub.add_parser("reset")
    a = ap.parse_args(argv)
    try:
        runner.wait_up(CURL, 5)
        runner.wait_up(PURL, 5)
    except RuntimeError:
        print("lab is not running: docker compose -f lab/docker-compose.yml up --build -d", file=sys.stderr)
        return 2
    runner.reset_faults(CURL, PURL)
    if a.cmd == "reset":
        print("faults cleared")
        return 0
    for f in LOG_DIR.glob("*.log"):  # the container user owns them; truncate instead of unlink
        f.write_text("", encoding="utf-8")
    try:
        d = runner.timeline(
            SCENARIOS[a.scenario], CURL, PURL, LOG_DIR, a.out, a.baseline, a.fault, runner.LoadGen(CURL), own_load=False
        )
    finally:
        runner.reset_faults(CURL, PURL)
    print(f"captured {a.scenario} -> {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
