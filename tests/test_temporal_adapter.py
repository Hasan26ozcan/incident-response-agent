"""Stage 17: the optional Temporal adapter. Server-backed tests skip when no Temporal server can be started."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("temporalio")

from temporalio import workflow  # noqa: E402
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment  # noqa: E402

from incident_agent.durable import temporal as tp  # noqa: E402
from incident_agent.durable import workflows as wf  # noqa: E402
from incident_agent.durable.engine import NonRetryable  # noqa: E402
from incident_agent.sandbox.backends import (
    CAPS,
    ENV,
    FILESYSTEM,
    NETWORK,
    OUTPUT,
    PROCESSES,
    TIME,
    ExecResult,
    ExecSpec,
)  # noqa: E402

DATA = {
    "meta": {"incident_id": "INC-777", "services": ["checkout-api"]},
    "logs": [
        {"timestamp": "t", "service": "checkout-api", "level": "ERROR", "message": "container OOMKilled by kubelet"},
    ],
    "metrics": {},
    "deploys": [],
}


class FakeBackend:
    name = "fake"
    guarantees = frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS})

    def available(self) -> tuple[bool, str]:
        return True, "fake"

    def run(self, spec: ExecSpec) -> ExecResult:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-S", "-B", "-c", spec.code], input=spec.stdin, capture_output=True, check=False
        )
        return ExecResult(proc.returncode, proc.stdout, proc.stderr, backend=self.name)


def deps() -> wf.Deps:
    return wf.Deps(backend_factory=FakeBackend, data_loader=lambda _i: DATA)


# ------------------------------------------------------------------------------------------ static checks (no server)


def test_workflow_definition_is_what_the_server_needs() -> None:
    defn = workflow._Definition.must_from_class(tp.IncidentWorkflow)  # noqa: SLF001
    assert defn.name == "IncidentWorkflow" and {"approve", "cancel"} <= set(defn.signals) and "progress" in defn.queries


def test_only_execute_is_single_attempt() -> None:
    assert tp.attempts_for("execute") == 1
    assert all(tp.attempts_for(n) == 3 for n in ("triage", "diagnose", "plan_action", "report"))


def test_the_first_approval_wins_and_cancel_is_remembered() -> None:
    w = tp.IncidentWorkflow()
    assert w.progress() == {"approval": None, "cancelled": False}
    w.approve({"approved": False, "by": "bob"})
    w.approve({"approved": True, "by": "mallory"})  # a later signal cannot overturn the first answer
    w.cancel()
    assert w.progress() == {"approval": {"approved": False, "by": "bob"}, "cancelled": True}


def test_the_step_order_is_the_engines_step_order() -> None:
    assert tuple(s.name for s in wf.incident_workflow().steps) == wf.STEP_ORDER
    assert set(tp.step_table()) == set(wf.STEP_ORDER) - {"approval"}


def test_the_workflow_sandbox_accepts_the_project_package() -> None:
    runner = tp.sandbox_runner()
    defn = workflow._Definition.must_from_class(tp.IncidentWorkflow)  # noqa: SLF001

    async def prepare() -> None:
        runner.prepare_workflow(defn)  # imports the workflow module inside the sandbox; raises when it is rejected

    asyncio.run(prepare())


def test_the_activity_runs_the_shared_step_functions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_RUNS_DIR", str(tmp_path))
    act = tp.make_activity(deps())
    env = ActivityEnvironment()
    inp = {"incident_id": "INC-777"}
    triage = asyncio.run(env.run(act, {"step": "triage", "run_id": "w1", "input": inp, "results": {}}))
    diagnose = asyncio.run(
        env.run(act, {"step": "diagnose", "run_id": "w1", "input": inp, "results": {"triage": triage}})
    )
    assert diagnose["category"] == "memory" and diagnose["service"] == "checkout-api"
    with pytest.raises(ValueError, match="unknown step"):
        asyncio.run(env.run(act, {"step": "approval", "run_id": "w1", "input": inp, "results": {}}))
    with pytest.raises(NonRetryable):
        asyncio.run(env.run(act, {"step": "triage", "run_id": "w1", "input": {}, "results": {}}))


def test_the_cli_parser() -> None:
    p = tp.build_parser()
    assert p.parse_args(["worker"]).cmd == "worker"
    assert p.parse_args(["start", "INC-002", "--workflow-id", "x"]).workflow_id == "x"
    assert p.parse_args(["signal", "x", "--by", "a", "--reject"]).reject is True
    with pytest.raises(SystemExit):
        p.parse_args(["signal", "x"])  # an approval needs a named approver


def test_the_cli_help_runs() -> None:
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "incident_agent.durable.temporal", "--help"], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0 and "worker" in out.stdout


class FakeHandle:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def signal(self, fn: Any, payload: Any) -> None:
        self.sent.append((fn, payload))

    async def describe(self) -> Any:
        class D:
            class status:  # noqa: N801
                name = "RUNNING"

        return D()


class FakeClient:
    def __init__(self) -> None:
        self.started: list[Any] = []
        self.handle = FakeHandle()

    async def start_workflow(self, fn: Any, arg: Any, *, id: str, task_queue: str) -> None:  # noqa: A002
        self.started.append((fn, arg, id, task_queue))

    def get_workflow_handle(self, wid: str) -> FakeHandle:
        return self.handle


def test_cli_commands_talk_to_the_client(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient()

    async def connect(address: str) -> FakeClient:
        return client

    ran: list[str] = []

    class FakeWorker:
        async def run(self) -> None:
            ran.append("worker")

    monkeypatch.setattr(tp.Client, "connect", staticmethod(connect))
    monkeypatch.setattr(tp, "build_worker", lambda c: FakeWorker())
    assert tp.main(["start", "INC-002", "--workflow-id", "wid-1"]) == 0
    assert "wid-1" in capsys.readouterr().out and client.started[0][1] == {"incident_id": "INC-002"}
    assert tp.main(["start", "INC-002"]) == 0 and client.started[1][2].startswith("incident-inc-002-")
    assert tp.main(["signal", "wid-1", "--by", "alice"]) == 0
    assert client.handle.sent[0][1] == {"approved": True, "by": "alice"}
    assert tp.main(["signal", "wid-1", "--by", "bob", "--reject"]) == 0
    assert client.handle.sent[1][1] == {"approved": False, "by": "bob"}
    assert tp.main(["status", "wid-1"]) == 0 and "RUNNING" in capsys.readouterr().out
    assert tp.main(["worker"]) == 0 and ran == ["worker"]


def test_cli_reports_connection_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    async def connect(address: str) -> Any:
        raise RuntimeError("Failed client connect")

    monkeypatch.setattr(tp.Client, "connect", staticmethod(connect))
    assert tp.main(["status", "x"]) == 2 and "error:" in capsys.readouterr().err


# ------------------------------------------------------------------------------------ server-backed (skipped here)


@pytest.fixture()
def env_factory() -> Any:
    async def start() -> WorkflowEnvironment:
        try:
            return await WorkflowEnvironment.start_local()
        except Exception as exc:  # noqa: BLE001 - no server can be started in this environment
            pytest.skip(f"no Temporal dev server available: {type(exc).__name__}")

    return start


def test_server_read_only_incident_completes(env_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_RUNS_DIR", str(tmp_path))
    quiet = {**DATA, "logs": []}

    async def go() -> dict[str, Any]:
        env = await env_factory()
        async with env:
            d = wf.Deps(backend_factory=FakeBackend, data_loader=lambda _i: quiet)
            async with tp.build_worker(env.client, d):
                wid = await tp.start_incident(env.client, "INC-777")
                return await env.client.get_workflow_handle(wid).result()  # type: ignore[no-any-return]

    out = asyncio.run(go())
    assert out["status"] == "completed" and "approval" not in out["results"]


def test_server_approval_signal_releases_the_wait(
    env_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENT_RUNS_DIR", str(tmp_path))

    async def go() -> dict[str, Any]:
        env = await env_factory()
        async with env:
            async with tp.build_worker(env.client, deps()):
                wid = await tp.start_incident(env.client, "INC-777")
                handle = env.client.get_workflow_handle(wid)
                await handle.signal(tp.IncidentWorkflow.approve, {"approved": True, "by": "alice"})
                return await handle.result()  # type: ignore[no-any-return]

    out = asyncio.run(go())
    assert out["results"]["execute"]["mode"] == "applied" and out["results"]["execute"]["approved_by"] == "alice"


def test_server_cancel_stops_the_workflow(env_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_RUNS_DIR", str(tmp_path))

    async def go() -> dict[str, Any]:
        env = await env_factory()
        async with env:
            async with tp.build_worker(env.client, deps()):
                wid = await tp.start_incident(env.client, "INC-777")
                handle = env.client.get_workflow_handle(wid)
                await handle.signal(tp.IncidentWorkflow.cancel)
                return await handle.result()  # type: ignore[no-any-return]

    assert asyncio.run(go())["status"] == "cancelled"


def test_server_unapproved_wait_times_out_in_time_skipping_env(
    env_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENT_RUNS_DIR", str(tmp_path))

    async def go() -> dict[str, Any]:
        try:
            env = await WorkflowEnvironment.start_time_skipping()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Temporal test server available: {type(exc).__name__}")
        async with env:
            async with tp.build_worker(env.client, deps()):
                wid = await tp.start_incident(env.client, "INC-777")
                return await env.client.get_workflow_handle(wid).result()  # type: ignore[no-any-return]

    out = asyncio.run(go())
    assert out["results"]["approval"]["timed_out"] is True and out["results"]["execute"]["mode"] == "dry_run"
