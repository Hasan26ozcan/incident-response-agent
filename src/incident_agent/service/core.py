# ruff: noqa: E501
"""The service without HTTP: submit, status, approve, health, metrics. Testable with fakes; the HTTP layer is thin."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incident_agent.durable.engine import Engine
from incident_agent.durable.workflows import Deps, incident_workflow
from incident_agent.hitl.bridge import Controller
from incident_agent.hitl.gate import ApprovalGate, GateError
from incident_agent.hitl.notify import MultiNotifier, Notifier, OutboxNotifier, SlackNotifier
from incident_agent.hitl.policy import DEFAULT_POLICY
from incident_agent.hitl.roster import AuthError, Roster
from incident_agent.hitl.store import RequestStore, StoreTampered, check_request_id
from incident_agent.observe.redact import redact
from incident_agent.pipeline.run import ID_RE, MODES, PipelineConfig, investigate
from incident_agent.pipeline.stack import StackConfig
from incident_agent.reporting.sanitize import one_line
from incident_agent.security.killswitch import KillSwitch
from incident_agent.security.signing import ApprovalSigner, load_or_create_key

RUN_ID_RE = re.compile(r"[a-z]{3}-\d{3}-\d{6,12}(-\d+)?")
ACTIVE = ("queued", "running")
OPEN = ("queued", "running", "waiting")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")


@dataclass
class ServiceConfig:
    root: Path
    mode: str = "shadow"
    agent: str = "llm_react"
    cascade: bool = False
    max_queue: int = 20
    rate_per_minute: int = 30
    control_interval: float = 1.0
    client_factory: Callable[[], Any] | None = None
    backend_factory: Callable[[], Any] | None = None
    incident_exists: Callable[[str], bool] | None = None
    sender: Any = None  # payload sender with .send({"text": ...}); None = no run notifications
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")


def _default_exists(incident_id: str) -> bool:
    from incident_agent.tools import _incident_dir

    try:
        return _incident_dir(incident_id).is_dir()
    except ValueError:
        return False


class Service:
    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self.root = Path(cfg.root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.killswitch = KillSwitch(self.root / "killswitch", clock=cfg.clock)
        self.started = cfg.clock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=cfg.max_queue)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._hits: dict[str, deque[float]] = {}
        self.counters = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "rejected": 0,
            "auth_failed": 0,
            "control_errors": 0,
            "notify_failed": 0,
            "write_failed": 0,
            "recovered_interrupted": 0,
        }
        self.gate: ApprovalGate | None = None
        self.controller: Controller | None = None
        if cfg.mode == "assist":
            self._init_assist()
        self._recover()

    # ------------------------------------------------------------------------------------------ assist mode

    def _init_assist(self) -> None:
        signer = ApprovalSigner(load_or_create_key(self.root / "approval.key"))
        seal = load_or_create_key(self.root / "store.key")
        notifiers: list[Notifier] = [OutboxNotifier(self.root / "outbox.jsonl")]
        if self.cfg.sender is not None:
            notifiers.append(SlackNotifier(self.cfg.sender))
        self.roster = Roster(self.root / "roster.json")
        self.gate = ApprovalGate(
            RequestStore(self.root / "requests", self.cfg.clock, seal_key=seal),
            self.roster,
            DEFAULT_POLICY,
            MultiNotifier(notifiers),
            self.cfg.clock,
        )
        deps = Deps(signer=signer, approval_timeout=DEFAULT_POLICY.total_timeout)
        if self.cfg.backend_factory is not None:
            deps.backend_factory = self.cfg.backend_factory
        self.engine = Engine(incident_workflow(deps), self.runs_dir, poll_interval=0.1)
        self.controller = Controller(self.gate, self.engine, signer=signer)

    def _recover(self) -> None:
        """After a restart the job table is rebuilt from disk: finished runs stay visible, runs that were waiting for
        a human can still be completed, and runs the crash cut short are reported as interrupted (never silently lost)."""
        for d in sorted(self.runs_dir.iterdir()):
            if not (d.is_dir() and RUN_ID_RE.fullmatch(d.name)):
                continue
            disk = self._disk(d.name)
            inc = "-".join(d.name.split("-")[:2]).upper()
            job: dict[str, Any] = {
                "run_id": d.name,
                "incident_id": inc,
                "by": "recovered",
                "finished": self.cfg.clock(),
            }
            if disk is None:
                job.update(status="interrupted", error="the service stopped while this run was in progress")
                self.counters["recovered_interrupted"] += 1
            else:
                job.update(status=disk.get("status", "failed"), summary=disk.get("summary"))
            self.jobs[d.name] = job

    # ------------------------------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._worker, name="investigator", daemon=True)
        t.start()
        self._threads.append(t)
        if self.controller is not None:
            c = threading.Thread(target=self._control_loop, name="approval-controller", daemon=True)
            c.start()
            self._threads.append(c)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()

    def _control_loop(self) -> None:
        while not self._stop.wait(self.cfg.control_interval):
            try:
                if self.controller is not None and not self.killswitch.engaged():
                    self.controller.step()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad request
                self.counters["control_errors"] += 1

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._execute(run_id)

    def drain(self, timeout: float = 30.0) -> bool:
        """Wait until nothing is queued or running (tests and graceful shutdown)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not any(j["status"] in ACTIVE for j in self.jobs.values()):
                    return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------------------------------ jobs

    def _execute(self, run_id: str) -> None:
        job = self.jobs[run_id]
        job["status"] = "running"
        job["started"] = self.cfg.clock()
        try:
            client = self.cfg.client_factory() if self.cfg.client_factory else None
            pc = PipelineConfig(
                agent=self.cfg.agent, mode=self.cfg.mode, root=self.runs_dir, stack=StackConfig(self.cfg.cascade, None)
            )
            inv = investigate(
                job["incident_id"], pc, client=client, backend_factory=self.cfg.backend_factory, run_id=run_id
            )
            job.update(status=inv.status, summary=one_line(redact(inv.summary), 300), mode=self.cfg.mode)
            self.counters["completed"] += 1
        except Exception as exc:  # noqa: BLE001 - one failed investigation must not stop the service
            job.update(status="failed", error=one_line(redact(f"{type(exc).__name__}: {exc}"), 200))
            self.counters["failed"] += 1
        job["finished"] = self.cfg.clock()
        self._notify(job)

    def _notify(self, job: dict[str, Any]) -> None:
        if self.cfg.sender is None:
            return
        text = f"[{job['incident_id']}] investigation {job['status']} ({self.cfg.mode} mode): {job.get('summary') or job.get('error', '')}"
        try:
            self.cfg.sender.send({"text": one_line(text, 500)})
        except Exception:  # noqa: BLE001 - a notification failure never fails the investigation
            self.counters["notify_failed"] += 1

    def _rate_ok(self, who: str) -> bool:
        now = self.cfg.clock()
        q = self._hits.setdefault(who, deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.cfg.rate_per_minute:
            return False
        q.append(now)
        return True

    def submit(self, incident_id: Any, who: str) -> tuple[int, dict[str, Any]]:
        if not isinstance(incident_id, str) or not ID_RE.fullmatch(incident_id):
            return 400, {"error": "incident_id must look like INC-001 or LAB-001"}
        if self.killswitch.engaged():
            return 503, {"error": "kill switch engaged: no new investigations"}
        if not self._rate_ok(who):
            self.counters["rejected"] += 1
            return 429, {"error": "rate limit: slow down"}
        if not (self.cfg.incident_exists or _default_exists)(incident_id):
            return 404, {"error": "unknown incident"}
        with self._lock:
            for rid, j in list(self.jobs.items()):
                self._refresh(rid)
                if j["incident_id"] == incident_id and j["status"] in OPEN:
                    return 200, {"run_id": rid, "status": j["status"], "duplicate": True}
            run_id = f"{incident_id.lower()}-{int(self.cfg.clock())}"
            while run_id in self.jobs or (self.runs_dir / run_id).exists():
                run_id += "-1"
            job = {
                "run_id": run_id,
                "incident_id": incident_id,
                "status": "queued",
                "by": who,
                "queued": self.cfg.clock(),
            }
            try:
                self._queue.put_nowait(run_id)
            except queue.Full:
                self.counters["rejected"] += 1
                return 429, {"error": "queue is full, try again later"}
            self.jobs[run_id] = job
            self.counters["submitted"] += 1
        return 202, {"run_id": run_id, "status": "queued"}

    # ------------------------------------------------------------------------------------------ reading

    def _disk(self, run_id: str) -> dict[str, Any] | None:
        p = self.runs_dir / run_id / "investigation.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _refresh(self, run_id: str) -> None:
        """In assist mode a run keeps going after the investigation call returned: take the engine's word for it."""
        job = self.jobs.get(run_id)
        if self.controller is None or job is None or "finished" not in job or job.get("status") in TERMINAL:
            return
        try:
            st = self.engine.state(run_id)
        except (KeyError, ValueError):
            return
        if st.status != job["status"]:
            summary = (st.results.get("report") or {}).get("summary") or st.error or ""
            job.update(status=st.status, summary=one_line(str(summary), 300), finished=self.cfg.clock())
            self._write_disk(run_id, st)

    def _write_disk(self, run_id: str, st: Any) -> None:
        disk = self._disk(run_id) or {}
        disk.update(
            status=st.status, execution=st.results.get("execute") or {}, summary=self.jobs[run_id].get("summary")
        )
        try:
            (self.runs_dir / run_id / "investigation.json").write_text(
                json.dumps(disk, indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            self.counters["write_failed"] += 1

    def get_run(self, run_id: str) -> tuple[int, dict[str, Any]]:
        if not RUN_ID_RE.fullmatch(run_id):
            return 400, {"error": "bad run id"}
        self._refresh(run_id)
        job, disk = self.jobs.get(run_id), self._disk(run_id)
        if job is None and disk is None:
            return 404, {"error": "unknown run"}
        out: dict[str, Any] = dict(job or {"run_id": run_id})
        if disk:
            out.update(
                status=disk.get("status", out.get("status")),
                diagnosis=disk.get("diagnosis"),
                plan=disk.get("plan"),
                execution=disk.get("execution"),
                summary=disk.get("summary"),
                related=disk.get("related"),
                observability=disk.get("observability"),
            )
        return 200, out

    def list_runs(self, limit: int = 50) -> dict[str, Any]:
        for rid in list(self.jobs):
            self._refresh(rid)
        rows: dict[str, dict[str, Any]] = {
            rid: {k: j.get(k) for k in ("run_id", "incident_id", "status")} for rid, j in self.jobs.items()
        }
        dirs = [d for d in self.runs_dir.iterdir() if d.is_dir() and RUN_ID_RE.fullmatch(d.name)]
        for d in sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            disk = self._disk(d.name)
            if disk and d.name not in rows:
                inc = "-".join(d.name.split("-")[:2]).upper()
                rows[d.name] = {"run_id": d.name, "incident_id": inc, "status": disk.get("status")}
        return {"runs": list(rows.values())[-limit:][::-1], "mode": self.cfg.mode}

    # ------------------------------------------------------------------------------------------ approvals

    def list_requests(self) -> tuple[int, dict[str, Any]]:
        if self.gate is None:
            return 200, {"requests": [], "note": "approvals exist only in assist mode"}
        out = []
        for rid in self.gate.store.ids():
            try:
                st = self.gate.store.state(rid)
            except StoreTampered:
                out.append({"id": rid, "status": "tampered"})
                continue
            out.append(
                {
                    "id": rid,
                    "incident": st.incident_id,
                    "status": st.status,
                    "tier": st.tier,
                    "command": one_line(st.command_line, 200),
                    "fingerprint_prefix": st.fingerprint[:8],
                    "level": st.level_name,
                }
            )
        return 200, {"requests": out}

    def decide(
        self, request_id: str, approve: bool, name: str, token: str, confirm: str, comment: str
    ) -> tuple[int, dict[str, Any]]:
        if self.gate is None:
            return 409, {"error": "approvals exist only in assist mode"}
        try:
            check_request_id(request_id)
            if self.killswitch.engaged():
                return 503, {"error": "kill switch engaged"}
            if approve:
                st = self.gate.approve(request_id, name, token, confirm=confirm, comment=comment)
            else:
                st = self.gate.reject(request_id, name, token, comment=comment)
        except AuthError:
            self.counters["auth_failed"] += 1
            return 401, {"error": "authentication failed"}
        except KeyError:
            return 404, {"error": "unknown request"}
        except (GateError, ValueError, StoreTampered) as exc:
            return 409, {"error": one_line(str(exc), 200)}
        return 200, {"id": st.id, "status": st.status, "approvals": len(st.approvals), "required": st.required}

    # ------------------------------------------------------------------------------------------ health

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def ready(self) -> tuple[int, dict[str, Any]]:
        ks = self.killswitch.state()
        alive = bool(self._threads) and all(t.is_alive() for t in self._threads)
        ok = alive and self.runs_dir.is_dir()
        return (200 if ok else 503), {
            "status": "ready" if ok else "not ready",
            "mode": self.cfg.mode,
            "worker_alive": alive,
            "kill_switch": "engaged" if ks.engaged else "off",
            "queued": self._queue.qsize(),
            "uptime_s": round(self.cfg.clock() - self.started, 1),
        }

    def metrics(self) -> str:
        by: dict[str, int] = {}
        for j in self.jobs.values():
            by[j["status"]] = by.get(j["status"], 0) + 1
        lines = ["# TYPE incident_agent_jobs gauge"]
        lines += [f'incident_agent_jobs{{status="{k}"}} {v}' for k, v in sorted(by.items())]
        lines += ["# TYPE incident_agent_events_total counter"]
        lines += [f'incident_agent_events_total{{kind="{k}"}} {v}' for k, v in sorted(self.counters.items())]
        lines += [
            f"incident_agent_queue_depth {self._queue.qsize()}",
            f"incident_agent_kill_switch {int(self.killswitch.engaged())}",
        ]
        return "\n".join(lines) + "\n"
