# ruff: noqa: E501
"""Whole-system acceptance: every layer, one chain, offline (scripted model, real engine/sandbox/HTTP/processes).

    python -m incident_agent.acceptance [--quick] [--only NAME[,NAME]] [--json PATH]

Each step builds on real components, not mocks of them. A planted secret travels through the whole run and the last
step proves it is in no file, response or notification. Exit code 0 only if every step passes.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLANTED = "gsk_ACCEPTANCE0PLANTED0SECRET0VALUE0123456789"
MEM = "checkout-api retains memory until the heap limit is exceeded: OutOfMemory"
USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 400}

ROOT_CAUSES = {  # one scripted answer per lab incident: what a competent model would conclude
    "LAB-001": ("checkout-api CPU saturated by a busy loop: cpu_loop, high CPU usage", "checkout-api", False),
    "LAB-002": (MEM, "checkout-api", False),
    "LAB-003": (
        "payments-stub is slow; checkout-api timeouts waiting on the dependency payments-stub",
        "payments-stub",
        False,
    ),
    "LAB-004": (
        "bad deploy 2.4.1 of checkout-api: missing PAYMENT_API_URL config after the release",
        "checkout-api",
        False,
    ),
    "LAB-005": ("planned load test, not an incident", "checkout-api", True),
}


def scripted(incident_id: str, model: str = "openai/gpt-oss-120b") -> Any:
    from incident_agent.llm import LLMResponse, ScriptedClient, ToolCall

    cause, service, false_alarm = ROOT_CAUSES[incident_id]

    def j(obj: Any) -> Any:
        return LLMResponse(content=json.dumps(obj), usage=dict(USAGE))

    plan = {
        "hypotheses": ["a", "b"],
        "steps": [{"tool": "log_overview", "purpose": "mix"}, {"tool": "get_metric_summary", "purpose": "t"}],
    }
    calls = LLMResponse(
        tool_calls=[
            ToolCall(id="c0", name="log_overview", arguments={}),
            ToolCall(id="c1", name="get_metric_summary", arguments={}),
        ],
        usage=dict(USAGE),
    )
    final = j(
        {
            "is_false_alarm": false_alarm,
            "root_cause": cause,
            "confidence": 0.8,
            "evidence": [
                {"source_type": "log_entry", "source": "logs.log", "detail": "evidence line", "timestamp": ""}
            ],
            "affected_service": service,
            "category": "x",
            "reasoning_steps": ["r1", "r2"],
            "recommendation": "Do the sensible thing now.",
            "risk_tier": "high",
        }
    )
    return ScriptedClient([j(plan), calls, LLMResponse(content="ready", usage=dict(USAGE)), final], model=model)


class Ctx:
    def __init__(self, tmp: Path, quick: bool) -> None:
        self.tmp, self.quick = tmp, quick
        self.responses: list[str] = []  # every HTTP body and notification, scanned for the secret at the end
        self.state: dict[str, Any] = {}

    def sub(self, name: str) -> Path:
        p = self.tmp / name
        p.mkdir(parents=True, exist_ok=True)
        return p


STEPS: list[tuple[str, str, bool, Callable[[Ctx], str]]] = []  # (name, title, slow, fn)


def step(name: str, title: str, slow: bool = False) -> Callable[[Callable[[Ctx], str]], Callable[[Ctx], str]]:
    def deco(fn: Callable[[Ctx], str]) -> Callable[[Ctx], str]:
        STEPS.append((name, title, slow, fn))
        return fn

    return deco


def need(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ------------------------------------------------------------------------------------------------ steps


@step("imports", "every module of the package imports")
def s_imports(_c: Ctx) -> str:
    import incident_agent

    ok = 0
    optional: list[str] = []
    bad: list[str] = []
    for m in pkgutil.walk_packages(incident_agent.__path__, "incident_agent."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
            ok += 1
        except ModuleNotFoundError as exc:
            (bad if (exc.name or "").startswith("incident_agent") else optional).append(f"{m.name} ({exc.name})")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{m.name}: {type(exc).__name__}: {exc}")
    need(not bad, f"broken imports: {bad[:3]}")
    return f"{ok} modules" + (f", {len(optional)} need optional packages" if optional else "")


@step("data", "dataset, lab captures and gold files are present and consistent")
def s_data(_c: Ctx) -> str:
    from incident_agent.lab.scenarios import SCENARIOS
    from incident_agent.tools import _incident_dir

    for sid in SCENARIOS:
        d = _incident_dir(sid)
        need(d.is_dir() and any(d.iterdir()), f"{sid} capture missing")
        need((d.parent / "gold" / f"{sid}.json").exists(), f"{sid} gold missing")
    inc = sorted(p.name for p in _incident_dir("INC-001").parent.glob("INC-*"))
    need(len(inc) >= 14, f"only {len(inc)} dataset incidents")
    return f"{len(inc)} dataset incidents, {len(SCENARIOS)} lab captures"


@step("lab", "fault lab: real processes, injected fault, captured incident folder", slow=True)
def s_lab(c: Ctx) -> str:
    from incident_agent.lab import runner

    out = c.sub("lab-capture")
    d = runner.run_local("LAB-002", out=out, baseline_s=3.0, fault_s=6.0)
    files = sorted(p.name for p in Path(d).iterdir())
    need(len(files) >= 3, f"capture has only {files}")
    logs = " ".join(p.read_text(errors="replace") for p in Path(d).iterdir() if p.is_file())
    need("checkout-api" in logs, "captured logs do not mention the service")
    return f"captured {len(files)} files from live processes"


@step("pipeline", "pipeline on all five lab incidents: right action class, shadow never changes anything")
def s_pipeline(c: Ctx) -> str:
    from incident_agent.agenteval.rig import RecordingBackend
    from incident_agent.pipeline.run import PipelineConfig, investigate

    backend = RecordingBackend()
    verdicts = {}
    for iid in ROOT_CAUSES:
        inv = investigate(
            iid,
            PipelineConfig(root=c.sub("pipeline")),
            client=scripted(iid),
            backend_factory=lambda: backend,
            run_id=f"{iid.lower()}-1",
        )
        need(inv.status == "completed", f"{iid} ended {inv.status}: {inv.summary}")
        need(inv.execution.get("changed") is False, f"{iid} changed something in shadow mode")
        need(
            (inv.run_dir / "trace.jsonl").exists() and (inv.run_dir / "observability.html").exists(),
            f"{iid} has no trace or dashboard",
        )
        verdicts[iid] = inv.plan.get("verdict")
    need(backend.applied == 0, "shadow mode applied a command")
    need(
        verdicts["LAB-002"] == "needs_approval",
        f"memory leak should propose a gated restart, got {verdicts['LAB-002']}",
    )
    need(
        verdicts["LAB-003"] != "needs_approval" and verdicts["LAB-005"] != "needs_approval",
        f"dependency/false alarm must not propose a mutation: {verdicts}",
    )
    c.state["pipeline_verdicts"] = verdicts
    return ", ".join(f"{k[-3:]}:{v}" for k, v in verdicts.items())


@step("security", "adversarial suite: nothing vulnerable, closed findings stay closed")
def s_security(_c: Ctx) -> str:
    from incident_agent.security import attacks

    res = attacks.run_attacks()
    s = attacks.summarise(res)
    need(not s["vulnerable"], f"vulnerable: {s['vulnerable']}")
    need(not s["gaps"], f"open gaps: {s['gaps']}")
    for aid in ("INJ-09", "APP-08", "INJ-12", "INJ-13"):
        need(next(r for r in res if r.id == aid).verdict == "blocked", f"{aid} is not blocked")
    return str(s["verdicts"])


def _http(
    port: int, method: str, path: str, token: str | None = None, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310  # nosec B310 - fixed loopback http URL
            txt = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        txt, code = e.read().decode(), e.code
    return code, (json.loads(txt) if txt.startswith(("{", "[")) else txt)


def store_of(live: Any) -> Any:
    gate = live.svc.gate
    need(gate is not None, "assist mode has no approval gate")
    return gate.store


class Live:
    """A running service on a real socket."""

    def __init__(self, root: Path, c: Ctx, mode: str, backend: Any, client_factory: Callable[[], Any]) -> None:
        from incident_agent.service.core import Service, ServiceConfig
        from incident_agent.service.http import make_server
        from incident_agent.service.tokens import TokenStore

        class Sender:
            def send(_s, payload: Any) -> Any:  # noqa: N805
                c.responses.append(json.dumps(payload))
                return type("R", (), {"ok": True})()

        self.tokens = TokenStore(root / "tokens.json")
        self.read = self.tokens.add("viewer", ["read"]) if not self.tokens.names() else c.state["read"]
        self.sub = self.tokens.add("ci", ["read", "submit"]) if "ci" not in self.tokens.names() else c.state["sub"]
        c.state.update(read=self.read, sub=self.sub)
        self.svc = Service(
            ServiceConfig(
                root=root,
                mode=mode,
                client_factory=client_factory,
                backend_factory=lambda: backend,
                sender=Sender(),
                control_interval=0.05,
            )
        )
        self.srv = make_server(self.svc, self.tokens, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.svc.start()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.c = c

    def call(self, *a: Any, **k: Any) -> tuple[int, Any]:
        code, body = _http(self.port, *a, **k)
        self.c.responses.append(json.dumps(body) if not isinstance(body, str) else body)
        return code, body

    def wait(self, run_id: str, status: tuple[str, ...], timeout: float = 40.0) -> dict[str, Any]:
        end = time.time() + timeout
        body: dict[str, Any] = {}
        while time.time() < end:
            _c, body = self.call("GET", f"/api/runs/{run_id}", self.read)
            if body.get("status") in status:
                return body
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} never reached {status}: {str(body)[:200]}")

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.svc.stop()


@step(
    "service-shadow", "service in shadow mode over HTTP: auth, submit, status, metrics, kill-switch, leak-free errors"
)
def s_service_shadow(c: Ctx) -> str:
    from incident_agent.agenteval.rig import RecordingBackend
    from incident_agent.security.killswitch import KillSwitch

    backend = RecordingBackend()
    live = Live(c.sub("svc-shadow"), c, "shadow", backend, lambda: scripted("LAB-002"))
    try:
        need(live.call("GET", "/healthz")[0] == 200 and live.call("GET", "/readyz")[1]["worker_alive"], "health/ready")
        need(
            live.call("GET", "/api/runs")[0] == 401 and live.call("GET", "/api/runs", "ira_x")[0] == 401,
            "API open without a token",
        )
        need(
            live.call("POST", "/api/incidents", live.read, {"incident_id": "LAB-002"})[0] == 403,
            "read token could submit",
        )
        code, b = live.call("POST", "/api/incidents", live.sub, {"incident_id": "LAB-002"})
        need(code == 202, f"submit -> {code}")
        run = live.wait(b["run_id"], ("completed", "failed"))
        need(
            run["status"] == "completed" and backend.applied == 0,
            f"shadow run {run['status']}, applied={backend.applied}",
        )
        need(run["plan"]["verdict"] == "needs_approval" and run["execution"].get("changed") is False, "plan/execution")
        need("incident_agent_jobs" in live.call("GET", "/metrics", live.read)[1], "metrics")
        # a failing provider whose error text carries a secret: reported, redacted, service stays up
        live.svc.cfg.client_factory = lambda: (_ for _ in ()).throw(RuntimeError(f"provider rejected key {PLANTED}"))
        code, b2 = live.call("POST", "/api/incidents", live.sub, {"incident_id": "LAB-005"})
        failed = live.wait(b2["run_id"], ("failed",))
        need(
            PLANTED not in json.dumps(failed) and live.call("GET", "/healthz")[0] == 200,
            "secret leaked or service died",
        )
        KillSwitch(live.svc.root / "killswitch").engage("acceptance", "ops")
        need(
            live.call("POST", "/api/incidents", live.sub, {"incident_id": "LAB-001"})[0] == 503,
            "kill-switch did not stop new work",
        )
        need(live.call("GET", "/readyz")[1]["kill_switch"] == "engaged", "ready does not show the switch")
    finally:
        live.close()
    return "auth, submit, redaction, kill-switch ok; nothing applied"


@step(
    "service-assist",
    "assist mode: human approval over HTTP, sealed, applied exactly once, survives a restart, forgery refused",
)
def s_service_assist(c: Ctx) -> str:
    from incident_agent.agenteval.rig import RecordingBackend
    from incident_agent.durable.journal import Journal

    root, backend = c.sub("svc-assist"), RecordingBackend()
    live = Live(root, c, "assist", backend, lambda: scripted("LAB-002"))
    alice = live.svc.roster.add("alice", ["oncall"])
    code, b = live.call("POST", "/api/incidents", live.sub, {"incident_id": "LAB-002"})
    need(code == 202, "submit")
    live.wait(b["run_id"], ("waiting",))
    reqs: list[dict[str, Any]] = []
    for _ in range(200):
        reqs = live.call("GET", "/api/requests", live.read)[1]["requests"]
        if reqs:
            break
        time.sleep(0.1)
    need(bool(reqs) and reqs[0]["status"] == "pending" and backend.applied == 0, "approval request not opened")
    rid = reqs[0]["id"]
    fp = store_of(live).state(rid).fingerprint[:8]
    live.close()  # --- the whole service goes away while the request is open ---

    live = Live(
        root, c, "assist", backend, lambda: scripted("LAB-002")
    )  # --- and comes back on the same state directory ---
    try:
        path, hdr = f"/api/requests/{rid}/approve", {"X-Approver": "alice", "X-Approver-Token": alice}
        need(
            live.call(
                "POST", path, body={"confirm": fp}, headers={"X-Approver": "alice", "X-Approver-Token": live.sub}
            )[0]
            == 401,
            "service token approved",
        )
        need(live.call("POST", path, body={"confirm": "00000000"}, headers=hdr)[0] == 409, "wrong fingerprint accepted")
        need(backend.applied == 0, "applied before a valid approval")
        code, body = live.call("POST", path, body={"confirm": fp}, headers=hdr)
        need(code == 200 and body["status"] == "approved", f"approve -> {code} {body}")
        done = live.wait(b["run_id"], ("completed",))
        need(
            done["execution"].get("mode") == "applied" and backend.applied == 1,
            f"expected one applied action, got {backend.applied}",
        )
        need(
            live.call("POST", path, body={"confirm": fp}, headers=hdr)[0] == 409 and backend.applied == 1,
            "a second approval re-ran the action",
        )
        # forgery: a second request whose journal gets a line written without the seal key
        code, b2 = live.call("POST", "/api/incidents", live.sub, {"incident_id": "LAB-002"})
        run2 = live.wait(b2["run_id"], ("waiting",))
        rid2 = f"{b2['run_id']}.approval"
        for _ in range(200):
            if store_of(live).exists(rid2):
                break
            time.sleep(0.1)
        Journal(store_of(live).dir(rid2)).append(
            "resolved", outcome="approved", by=["mallory"], at=time.time(), reason="forged"
        )
        time.sleep(1.0)
        need(
            backend.applied == 1 and live.wait(b2["run_id"], ("waiting", "failed", "running"))["status"] != "completed",
            "forged approval was honoured",
        )
        need(run2["status"] == "waiting", "second run state")
    finally:
        live.close()
    return "approved by a human, applied once, restart-safe, forged store line refused"


@step("measure", "measurement tooling: real agent loop on lab data -> scored run, cost, dashboard, report")
def s_measure(c: Ctx) -> str:
    from incident_agent.eval import harness
    from incident_agent.measure import report, runner
    from incident_agent.measure.campaign import Config

    def factory(_model: str, _tracer: Any) -> Any:
        return None

    def adapter_factory(agent: str, _client: Any) -> Callable[[str], Any]:
        def diagnose(incident_id: str) -> Any:
            with harness.use_client(scripted(incident_id)):
                return harness.make_adapter(agent)(incident_id)

        return diagnose

    out = c.sub("measure")
    cfg = Config("react-120b", "llm_react", "groq/openai/gpt-oss-120b")
    path = runner.run_config(cfg, "lab", out, client_factory=factory, adapter_factory=adapter_factory)
    body = json.loads(path.read_text())
    need(body["summary"]["crashed"] == 0 and len(body["incidents"]) == 5, f"measure summary {body['summary']}")
    need((out / "react-120b__lab.observability.html").exists(), "no measurement dashboard")
    md = report.render_markdown(report.build(out))
    need("react-120b" in md and "F1" in md, "report incomplete")
    (out / "MEASUREMENTS.md").write_text(md, encoding="utf-8")
    return f"5 lab incidents scored, mean F1 {body['summary'].get('root_cause_f1')}"


@step("gate", "agentic eval gate against the committed baseline", slow=True)
def s_gate(_c: Ctx) -> str:
    r = subprocess.run(
        [sys.executable, "-m", "incident_agent.agenteval", "run"],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )  # noqa: S603
    need(r.returncode == 0 and "RESULT PASS" in r.stdout, f"gate failed:\n{r.stdout[-600:]}{r.stderr[-300:]}")
    return "RESULT PASS"


@step("no-leaks", "the planted secret is in no file, HTTP response or notification produced by this run")
def s_leaks(c: Ctx) -> str:
    hits = [r[:80] for r in c.responses if PLANTED in r]
    need(not hits, f"secret in a response/notification: {hits[:1]}")
    scanned = 0
    for p in c.tmp.rglob("*"):
        if p.is_file() and p.stat().st_size < 20_000_000:
            try:
                data = p.read_bytes()
            except OSError:  # a lock file held open by the engine (Windows): it holds no text
                continue
            scanned += 1
            need(PLANTED.encode() not in data, f"secret found in {p.relative_to(c.tmp)}")
    need(scanned > 20, f"only {scanned} files produced, the run did not do its work")
    return f"{scanned} files and {len(c.responses)} responses scanned"


# ------------------------------------------------------------------------------------------------ runner


def run(quick: bool = False, only: list[str] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    previous = {k: os.environ.get(k) for k in ("GROQ_API_KEY",)}  # captured BEFORE planting, restored afterwards
    os.environ["GROQ_API_KEY"] = PLANTED  # planted: must never surface anywhere
    with tempfile.TemporaryDirectory(prefix="acceptance-", ignore_cleanup_errors=True) as tmp:
        ctx = Ctx(Path(tmp), quick)
        try:
            for name, title, slow, fn in STEPS:
                if only and name not in only:
                    continue
                if quick and slow:
                    results.append(
                        {"name": name, "title": title, "status": "skipped", "detail": "--quick", "seconds": 0.0}
                    )
                    continue
                t0 = time.time()
                try:
                    detail, status = fn(ctx), "ok"
                except Exception as exc:  # noqa: BLE001 - a crashing step is a failing step
                    detail, status = (
                        (str(exc) or type(exc).__name__)
                        if isinstance(exc, AssertionError)
                        else "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                        "FAIL",
                    )
                results.append(
                    {
                        "name": name,
                        "title": title,
                        "status": status,
                        "detail": detail,
                        "seconds": round(time.time() - t0, 1),
                    }
                )
        finally:
            for k, v in previous.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incident_agent.acceptance")
    ap.add_argument("--quick", action="store_true", help="skip the slow steps (live processes, eval gate)")
    ap.add_argument("--only", default="", help="comma-separated step names")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    res = run(a.quick, [x for x in a.only.split(",") if x] or None)
    for r in res:
        mark = {"ok": "OK  ", "FAIL": "FAIL", "skipped": "skip"}[r["status"]]
        print(f"[{mark}] {r['name']:<14} {r['seconds']:>6.1f}s  {r['title']}\n         {r['detail']}")
    bad = [r for r in res if r["status"] == "FAIL"]
    ran = [r for r in res if r["status"] != "skipped"]
    print(f"\nACCEPTANCE {'FAIL' if bad else 'PASS'}: {len(ran) - len(bad)}/{len(ran)} steps")
    if a.json:
        a.json.write_text(json.dumps(res, indent=2), encoding="utf-8")
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
