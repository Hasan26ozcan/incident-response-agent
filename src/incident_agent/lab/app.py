# ruff: noqa: E501
"""A small HTTP service used as the system under investigation (``checkout-api``) and its dependency (``payments-stub``).

Real behaviour, not canned text: CPU burns, memory grows, requests time out against a slow dependency, a bad deploy raises
real exceptions. Faults are switched through ``/admin``; ``/metrics`` reports what the process actually did.
Run:  python -m incident_agent.lab.app --service checkout-api --port 8080 --dep http://127.0.0.1:8081
Logs use the dataset line format:  <iso ts> service=<name> level=<LVL> msg="..."
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import sys
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FAULTS = ("cpu_loop", "memory_leak", "slow", "bad_config")
LEAK_BYTES = 2 * 1024 * 1024
HEAP_LIMIT_MB = 96.0
DEP_TIMEOUT_S = 1.0
SLOW_DELAY_S = 2.5
BURN_S = 0.12


def iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, UTC).isoformat(timespec="seconds")


class Service:
    def __init__(self, name: str, dep: str | None = None, log_dir: Path | None = None, version: str = "2.3.0") -> None:
        self.name, self.dep, self.version = name, dep, version
        self.fault: str | None = None
        self.lock = threading.Lock()
        self.leak: list[bytearray] = []
        self.reqs: deque[tuple[float, float, int]] = deque(maxlen=5000)  # (t, latency_ms, status)
        self.snap: dict[str, float] = {
            "cpu_percent": 0.0,
            "latency_p99_ms": 0.0,
            "mem_mb": 0.0,
            "error_rate_percent": 0.0,
            "rps": 0.0,
        }
        self.deploys: list[dict[str, str]] = []
        self.rng = random.Random(7)
        self.n = 0
        self.stop = threading.Event()
        self.log_file: Path | None = None
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_file = log_dir / f"{name}.log"
        if not tracemalloc.is_tracing():
            tracemalloc.start()

    # ------------------------------------------------------------------ logging
    def log(self, level: str, msg: str) -> None:
        line = f'{iso()} service={self.name} level={level} msg="{msg.replace(chr(34), chr(39)).replace(chr(10), " ")}"'
        with self.lock:
            print(line, flush=True)
            if self.log_file:
                with self.log_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")

    # ------------------------------------------------------------------ admin
    def set_fault(self, name: str | None) -> None:
        if name is not None and name not in FAULTS:
            raise ValueError(f"unknown fault {name!r}")
        with self.lock:
            self.fault = name
            if name != "memory_leak":
                self.leak = []

    def deploy(self, version: str, description: str, *, broken: bool = False) -> None:
        with self.lock:
            self.version = version
            self.deploys.append(
                {
                    "deploy_id": f"deploy-{len(self.deploys) + 1:03d}",
                    "timestamp": iso(),
                    "service": self.name,
                    "version": version,
                    "description": description,
                }
            )
        self.log("INFO", f"deployed version {version}")
        if broken:
            self.set_fault("bad_config")

    # ------------------------------------------------------------------ request handling
    def _burn(self, seconds: float) -> None:
        end = time.process_time() + seconds
        x = 0
        while time.process_time() < end:
            x += 1

    def _call_dep(self) -> int:
        if not self.dep:
            return 200
        try:
            with urllib.request.urlopen(self.dep + "/charge", timeout=DEP_TIMEOUT_S) as r:  # noqa: S310  # nosec B310 - loopback lab URL
                return int(r.status)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            reason = "timeout" if "timed out" in str(exc) else "unreachable"
            self.log("ERROR", f"upstream payments-stub {reason} after {int(DEP_TIMEOUT_S * 1000)}ms")
            return 504

    def handle(self, path: str) -> tuple[int, dict[str, Any]]:
        t0 = time.perf_counter()
        status = 200
        with self.lock:
            self.n += 1
            n, fault = self.n, self.fault
        try:
            if path == "/charge":  # the dependency role
                time.sleep(SLOW_DELAY_S if fault == "slow" else 0.01 + self.rng.random() * 0.02)
            elif fault == "bad_config":
                raise KeyError("PAYMENT_API_URL")
            elif fault == "cpu_loop":
                self._burn(BURN_S)
                if n % 3 == 0:
                    self.log("WARN", f"possible unbounded loop detected in retry handler (attempt {n})")
            elif fault == "memory_leak":
                with self.lock:
                    self.leak.append(bytearray(LEAK_BYTES))
                mb = self._mem_mb()
                if mb > HEAP_LIMIT_MB:
                    status = 500
                    self.log("ERROR", f"OutOfMemory: heap limit {int(HEAP_LIMIT_MB)}MB exceeded, allocation failed")
                    with self.lock:
                        self.leak = self.leak[len(self.leak) // 2 :]  # the 'process' sheds memory after failing
                elif mb > HEAP_LIMIT_MB * 0.7 and n % 4 == 0:
                    self.log(
                        "WARN", f"heap usage {int(mb)}MB approaching limit {int(HEAP_LIMIT_MB)}MB; GC pauses increasing"
                    )
                time.sleep(min(mb / HEAP_LIMIT_MB, 1.0) * 0.15)
            else:
                time.sleep(0.02 + self.rng.random() * 0.04)
                status = self._call_dep()
        except KeyError as exc:
            status = 500
            self.log("ERROR", f"KeyError: {exc} missing in config for version {self.version}; request failed")
        ms = (time.perf_counter() - t0) * 1000
        with self.lock:
            self.reqs.append((time.time(), ms, status))
        if status == 200 and fault is None and n % 25 == 0:
            self.log("INFO", f"request processed in {int(ms)}ms")
        return status, {"status": status, "version": self.version}

    # ------------------------------------------------------------------ metrics
    def _mem_mb(self) -> float:
        return tracemalloc.get_traced_memory()[0] / 1e6

    def sample(self, last_cpu: float, last_wall: float) -> tuple[float, float]:
        now_cpu, now_wall = time.process_time(), time.time()
        cpu = min(max((now_cpu - last_cpu) / max(now_wall - last_wall, 1e-6) * 100, 0.0), 100.0)
        with self.lock:
            recent = [r for r in self.reqs if r[0] >= now_wall - 10]
        lat = sorted(r[1] for r in recent)
        p99 = lat[min(int(len(lat) * 0.99), len(lat) - 1)] if lat else 0.0
        err = 100.0 * sum(1 for r in recent if r[2] >= 500) / len(recent) if recent else 0.0
        self.snap = {
            "cpu_percent": round(cpu, 2),
            "latency_p99_ms": round(p99, 2),
            "mem_mb": round(self._mem_mb(), 2),
            "error_rate_percent": round(err, 2),
            "rps": round(len(recent) / 10, 2),
        }
        return now_cpu, now_wall

    def sampler(self, every: float = 1.0) -> None:
        c, w = time.process_time(), time.time()
        while not self.stop.wait(every):
            c, w = self.sample(c, w)


def make_handler(svc: Service) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # the service logs itself
            return

        def _send(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            with contextlib.suppress(
                BrokenPipeError, ConnectionResetError
            ):  # the client gave up (timeout): not our error
                self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "fault": svc.fault})
            elif self.path == "/metrics":
                self._send(200, {**svc.snap, "version": svc.version, "service": svc.name})
            elif self.path == "/deploys":
                self._send(200, {"deploys": svc.deploys})
            elif self.path in ("/checkout", "/charge"):
                code, body = svc.handle(self.path)
                self._send(code, body)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                if self.path == "/admin/fault":
                    svc.set_fault(data.get("name"))
                elif self.path == "/admin/deploy":
                    svc.deploy(str(data["version"]), str(data.get("description", "")), broken=bool(data.get("broken")))
                else:
                    return self._send(404, {"error": "not found"})
            except (ValueError, KeyError) as exc:
                return self._send(400, {"error": str(exc)})
            self._send(200, {"ok": True})

    return H


def serve(svc: Service, host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(svc))
    srv.daemon_threads = True
    threading.Thread(target=svc.sampler, daemon=True).start()
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="checkout-api")
    ap.add_argument("--host", default=os.environ.get("LAB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LAB_PORT", "8080")))
    ap.add_argument("--dep", default=os.environ.get("LAB_DEP"))
    ap.add_argument(
        "--log-dir", type=Path, default=Path(os.environ["LAB_LOG_DIR"]) if os.environ.get("LAB_LOG_DIR") else None
    )
    a = ap.parse_args(argv)
    svc = Service(a.service, a.dep, a.log_dir)
    srv = serve(svc, a.host, a.port)
    svc.log("INFO", f"service started version {svc.version} on port {a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
