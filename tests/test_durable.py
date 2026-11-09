"""Stage 17: the durable engine, the incident workflow and the CLI (no external service needed)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from incident_agent.durable import workflows as wf
from incident_agent.durable.__main__ import describe, main
from incident_agent.durable.engine import (
    Clock,
    Engine,
    NonRetryable,
    RetryPolicy,
    Step,
    StepContext,
    StepTimeout,
    Wait,
    Workflow,
    WorkflowMismatch,
    crash_after,
)
from incident_agent.durable.journal import (
    Journal,
    JournalError,
    LockError,
    RunLock,
    check_run_id,
    read_signals,
    write_signal,
)
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
)

FAST = RetryPolicy(max_attempts=3, initial_delay=0.5, backoff=2.0, max_delay=10.0)


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Crash(BaseException):
    """Stands in for a process death inside the engine (not caught by ``except Exception``)."""


def engine(flow: Workflow, root: Path, clock: Clock | None = None, hook: Any = None) -> Engine:
    return Engine(flow, root, clock=clock or FakeClock(), crash_hook=hook, poll_interval=0.5)


def kinds(e: Engine, run_id: str) -> list[str]:
    return [x["kind"] for x in e._journal(run_id).events()]  # noqa: SLF001


def one_step(name: str = "s", fn: Any = None, retry: RetryPolicy = FAST) -> Workflow:
    return Workflow("w", "1", (Step(name, fn or (lambda ctx: {"ok": True}), retry),))


# ------------------------------------------------------------------------------------------------ journal


def test_run_ids() -> None:
    assert check_run_id("inc-001.a_b") == "inc-001.a_b"
    for bad in ("", "../x", "a/b", "-x", "x" * 65, "end.", ".hidden", 5, "a b"):
        with pytest.raises(ValueError):
            check_run_id(bad)  # type: ignore[arg-type]


def test_journal_append_and_read(tmp_path: Path) -> None:
    j = Journal(tmp_path / "r")
    assert not j.exists() and j.events() == []
    a = j.append("one", x=1)
    b = j.append("two")
    assert (a["seq"], b["seq"]) == (1, 2) and j.exists()
    assert [e["kind"] for e in j.events()] == ["one", "two"]
    assert oct(j.path.stat().st_mode & 0o777) == "0o600" or os.name == "nt"


def test_torn_tail_is_ignored_and_truncated(tmp_path: Path) -> None:
    for tail in (b'{"seq": 3, "ki', b'{"seq": 3, "ts": 1, "kind": "x"}', b"garbage\n"):
        j = Journal(tmp_path / f"r{len(tail)}")
        j.append("a")
        j.append("b")
        with j.path.open("ab") as fh:
            fh.write(tail)
        assert [e["seq"] for e in j.events()] == [1, 2]
        j.append("c")
        assert [e["seq"] for e in j.events()] == [1, 2, 3]
        assert j.path.read_bytes().endswith(b"\n") and b"garbage" not in j.path.read_bytes()


def test_corruption_in_the_middle_refuses_to_load(tmp_path: Path) -> None:
    j = Journal(tmp_path / "r")
    for k in "abc":
        j.append(k)
    lines = j.path.read_bytes().split(b"\n")
    lines[1] = b"not json"
    j.path.write_bytes(b"\n".join(lines))
    with pytest.raises(JournalError, match="corrupt at event 2"):
        j.events()
    # a gap in the sequence is corruption too
    j2 = Journal(tmp_path / "r2")
    for k in "abcd":
        j2.append(k)
    lines = j2.path.read_bytes().split(b"\n")
    j2.path.write_bytes(b"\n".join([lines[0], lines[2], lines[3], b""]))
    with pytest.raises(JournalError):
        j2.events()
    # a JSON value that is not an object is not an event
    j3 = Journal(tmp_path / "r3")
    j3.path.parent.mkdir()
    j3.path.write_bytes(b"[1]\n[2]\n[3]\n")
    with pytest.raises(JournalError):
        j3.events()


def test_journal_write_failure(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "journal.jsonl").mkdir()  # a directory where the file should be
    with pytest.raises(JournalError, match="cannot read"):
        Journal(tmp_path / "r").append("x")


def test_journal_write_failure_after_a_good_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    j = Journal(tmp_path / "r")
    j.append("a")

    def deny(*_a: Any, **_k: Any) -> int:
        raise PermissionError("read-only")

    monkeypatch.setattr("incident_agent.durable.journal.os.open", deny)
    with pytest.raises(JournalError, match="cannot write the journal: PermissionError"):
        j.append("b")


def test_signals_roundtrip_and_validation(tmp_path: Path) -> None:
    d = tmp_path / "r"
    assert read_signals(d) == []
    sid = write_signal(d, "approval", {"by": "a"})
    write_signal(d, "ping")
    with (d / "signals.jsonl").open("ab") as fh:
        fh.write(b"torn\n[1]\n" + b'{"id": 5, "name": "x"}\n')
    got = read_signals(d)
    assert [s["name"] for s in got] == ["approval", "ping"] and got[0]["id"] == sid and got[1]["payload"] == {}
    for bad in ("", "Approval", "1x", "a-b", "x" * 50):
        with pytest.raises(ValueError):
            write_signal(d, bad)
    with pytest.raises(ValueError, match="larger"):
        write_signal(d, "big", {"x": "y" * 5000})


# ------------------------------------------------------------------------------------------------ lock


def test_lock_is_exclusive_and_releasable(tmp_path: Path) -> None:
    a, b = RunLock(tmp_path / "r"), RunLock(tmp_path / "r")
    a.acquire()
    with pytest.raises(LockError):
        b.acquire()
    info = json.loads((tmp_path / "r" / "lock.json").read_text())
    assert info["pid"] == os.getpid()
    a.release()
    a.release()  # releasing twice is harmless
    b.acquire()
    b.release()
    RunLock(tmp_path / "never").release()  # never acquired: nothing to do


def test_lock_info_is_rewritten_not_appended(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "lock.json").write_text("x" * 500)
    lock = RunLock(tmp_path / "r")
    lock.acquire()
    assert json.loads((tmp_path / "r" / "lock.json").read_text())["pid"] == os.getpid()
    lock.release()


def test_lock_is_released_when_the_owner_dies(tmp_path: Path) -> None:
    code = (
        "import sys, time\n"
        "from incident_agent.durable.journal import RunLock\n"
        "l = RunLock(sys.argv[1]); l.acquire(); print('held', flush=True); time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "r")], stdout=subprocess.PIPE, text=True)  # noqa: S603
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        with pytest.raises(LockError):
            RunLock(tmp_path / "r").acquire()
        proc.kill()  # terminate the process without running cleanup code
        proc.wait()
        mine = RunLock(tmp_path / "r")
        mine.acquire()
        mine.release()
    finally:
        proc.kill()


def test_lock_race_has_exactly_one_winner(tmp_path: Path) -> None:
    barrier = threading.Barrier(12)
    wins: list[RunLock] = []
    lost: list[int] = []

    def attempt() -> None:
        lock = RunLock(tmp_path / "r")
        barrier.wait()
        try:
            lock.acquire()
            wins.append(lock)
        except LockError:
            lost.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(lost) == 11
    wins[0].release()


# ------------------------------------------------------------------------------------------------ engine basics


def test_definitions_are_validated() -> None:
    with pytest.raises(ValueError):
        Workflow("w", "1", ())
    s = Step("a", lambda c: 1)
    with pytest.raises(ValueError):
        Workflow("w", "1", (s, s))
    for kw in ({"max_attempts": 0}, {"initial_delay": -1}, {"backoff": 0.5}, {"max_delay": -1}):
        with pytest.raises(ValueError):
            RetryPolicy(**kw)
    p = RetryPolicy(max_attempts=5, initial_delay=1.0, backoff=2.0, max_delay=3.0)
    assert [p.delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 3.0, 3.0] and p.delay(0) == 1.0


def test_steps_run_in_order_and_results_are_journalled(tmp_path: Path) -> None:
    seen: list[str] = []

    def mk(name: str) -> Any:
        def fn(ctx: StepContext) -> dict[str, Any]:
            seen.append(name)
            assert ctx.idempotency_key == f"r:{name}" and ctx.attempt == 1 and ctx.input == {"a": 1}
            return {"name": name, "before": sorted(ctx.results)}

        return fn

    flow = Workflow("w", "1", (Step("a", mk("a")), Step("b", mk("b"))))
    e = engine(flow, tmp_path)
    assert e.start("r", {"a": 1}) == "r"
    state = e.run("r")
    assert state.status == "completed" and seen == ["a", "b"] and state.results["b"]["before"] == ["a"]
    assert e.list_runs() == ["r"] and kinds(e, "r")[0] == "run_started" and kinds(e, "r")[-1] == "run_completed"
    assert e.run("r").status == "completed" and seen == ["a", "b"]  # nothing runs twice


def test_start_is_idempotent_and_guards_the_input(tmp_path: Path) -> None:
    e = engine(one_step(), tmp_path)
    e.start("r", {"x": 1})
    e.start("r", {"x": 1})
    assert kinds(e, "r") == ["run_started"]
    with pytest.raises(ValueError, match="different input"):
        e.start("r", {"x": 2})
    assert engine(one_step(), tmp_path / "none").list_runs() == []


def test_unknown_runs(tmp_path: Path) -> None:
    e = engine(one_step(), tmp_path)
    for call in (lambda: e.state("x"), lambda: e.run("x"), lambda: e.signal("x", "approval"), lambda: e.cancel("x")):
        with pytest.raises(KeyError):
            call()
    with pytest.raises(ValueError):
        e.start("../x", {})


def test_retries_with_backoff_then_success(tmp_path: Path) -> None:
    attempts: list[int] = []

    def flaky(ctx: StepContext) -> dict[str, int]:
        attempts.append(ctx.attempt)
        if ctx.attempt < 3:
            raise RuntimeError(f"boom {ctx.attempt} token=sk-abcdefghijklmnop")
        return {"n": ctx.attempt}

    clock = FakeClock()
    e = engine(one_step("f", flaky), tmp_path, clock)
    e.start("r", {})
    state = e.run("r")
    assert state.status == "completed" and attempts == [1, 2, 3] and clock.sleeps == [0.5, 1.0]
    failed = [x for x in e._journal("r").events() if x["kind"] == "step_failed"]  # noqa: SLF001
    assert [x["retryable"] for x in failed] == [True, True] and failed[0]["error"].startswith("RuntimeError: boom 1")


def test_exhausted_retries_fail_the_run_and_can_be_retried(tmp_path: Path) -> None:
    box = {"fixed": False, "calls": 0}

    def fn(ctx: StepContext) -> dict[str, bool]:
        box["calls"] += 1
        if not box["fixed"]:
            raise ConnectionError("down")
        return {"fine": True}

    e = engine(one_step("s", fn), tmp_path)
    e.start("r", {})
    state = e.run("r")
    assert state.status == "failed" and state.error == "ConnectionError: down"
    calls, n_events = box["calls"], len(kinds(e, "r"))
    assert e.run("r").status == "failed"  # failure is terminal until someone asks for a retry
    assert len(kinds(e, "r")) == n_events and box["calls"] == calls  # ...and the journal stays untouched
    box["fixed"] = True
    state = e.run("r", retry_failed=True)
    assert state.status == "completed" and state.results["s"] == {"fine": True} and state.error is None
    assert "run_resumed" in kinds(e, "r")


def test_non_retryable_and_bad_results_fail_at_once(tmp_path: Path) -> None:
    def refuse(ctx: StepContext) -> Any:
        raise NonRetryable("policy says no")

    def circular(ctx: StepContext) -> Any:
        x: list[Any] = []
        x.append(x)
        return x

    def unserialisable(ctx: StepContext) -> Any:
        return {"x": object()}

    def huge(ctx: StepContext) -> Any:
        return {"x": "y" * 300_000}

    def value_error(ctx: StepContext) -> Any:
        raise ValueError("plain value error")

    for i, (fn, expect, calls) in enumerate(
        [(refuse, "policy says no", 1), (circular, "not JSON", 1), (unserialisable, "not JSON", 1), (huge, "larger", 1)]
    ):
        n = {"c": 0}

        def counted(ctx: StepContext, fn: Any = fn, n: dict[str, int] = n) -> Any:
            n["c"] += 1
            return fn(ctx)

        e = engine(one_step("s", counted), tmp_path / str(i))
        e.start("r", {})
        state = e.run("r")
        assert state.status == "failed" and expect in (state.error or "") and n["c"] == calls
    # an ordinary ValueError is a failure like any other: retried
    n = {"c": 0}

    def counted_ve(ctx: StepContext) -> Any:
        n["c"] += 1
        return value_error(ctx)

    e = engine(one_step("s", counted_ve), tmp_path / "ve")
    e.start("r", {})
    assert e.run("r").status == "failed" and n["c"] == 3


def test_step_timeout_is_a_retryable_failure(tmp_path: Path) -> None:
    release = threading.Event()

    def slow(ctx: StepContext) -> Any:
        release.wait(5)
        return {}

    flow = Workflow("w", "1", (Step("s", slow, RetryPolicy(max_attempts=2), timeout=0.05),))
    e = engine(flow, tmp_path)
    e.start("r", {})
    state = e.run("r")
    release.set()
    assert state.status == "failed" and "StepTimeout" in (state.error or "")
    assert issubclass(StepTimeout, Exception)


def test_step_with_timeout_returns_value_and_reraises(tmp_path: Path) -> None:
    flow = Workflow("w", "1", (Step("s", lambda c: {"v": 1}, timeout=5.0),))
    e = engine(flow, tmp_path / "a")
    e.start("r", {})
    assert e.run("r").results["s"] == {"v": 1}

    def bad(ctx: StepContext) -> Any:
        raise NonRetryable("inside a thread")

    e2 = engine(Workflow("w", "1", (Step("s", bad, timeout=5.0),)), tmp_path / "b")
    e2.start("r", {})
    assert "inside a thread" in (e2.run("r").error or "")


def test_conditional_steps_are_skipped_and_remembered(tmp_path: Path) -> None:
    ran: list[str] = []

    def mk(name: str) -> Any:
        return lambda ctx: ran.append(name) or {"n": name}

    flow = Workflow(
        "w",
        "1",
        (
            Step("a", mk("a")),
            Step("b", mk("b"), when=lambda r: False),
            Wait("w1", "go", 5, when=lambda r: False),
            Step("c", mk("c"), when=lambda r: "a" in r),
        ),
    )
    e = engine(flow, tmp_path)
    e.start("r", {})
    state = e.run("r")
    assert ran == ["a", "c"] and state.skipped == ("b", "w1") and state.status == "completed"


# ------------------------------------------------------------------------------------------------ crash and resume


def test_crash_then_resume_never_repeats_finished_steps(tmp_path: Path) -> None:
    ran: list[str] = []

    def mk(name: str) -> Any:
        return lambda ctx: ran.append(name) or {"n": name}

    flow = Workflow("w", "1", (Step("a", mk("a")), Step("b", mk("b")), Step("c", mk("c"))))

    def hook(event: dict[str, Any]) -> None:
        if event["kind"] == "step_completed" and event["step"] == "b":
            raise Crash

    e = engine(flow, tmp_path, hook=hook)
    e.start("r", {})
    with pytest.raises(Crash):
        e.run("r")
    assert ran == ["a", "b"] and e.state("r").status == "running"
    resumed = engine(flow, tmp_path)  # a "new process"
    state = resumed.run("r")
    assert state.status == "completed" and ran == ["a", "b", "c"]


def test_interrupted_step_runs_again_with_a_marker(tmp_path: Path) -> None:
    seen: list[tuple[int, bool]] = []
    armed = {"crash": True}

    def hook(event: dict[str, Any]) -> None:
        if armed["crash"] and event["kind"] == "step_started":
            raise Crash

    def fn(ctx: StepContext) -> dict[str, int]:
        started = [x for x in Journal(ctx.run_dir).events() if x["kind"] == "step_started"]
        seen.append((ctx.attempt, started[-1]["interrupted_before"]))
        return {"ok": 1}

    e = engine(one_step("s", fn), tmp_path, hook=hook)
    e.start("r", {})
    with pytest.raises(Crash):
        e.run("r")
    armed["crash"] = False
    assert engine(one_step("s", fn), tmp_path).run("r").status == "completed"
    assert seen == [(2, True)]  # the attempt counter survived the crash


def test_a_step_that_keeps_killing_its_worker_ends_in_failed(tmp_path: Path) -> None:
    def hook(event: dict[str, Any]) -> None:
        if event["kind"] == "step_started":
            raise Crash

    flow = one_step("s", retry=RetryPolicy(max_attempts=2))
    for _ in range(2):
        e = engine(flow, tmp_path, hook=hook)
        e.start("r", {})
        with pytest.raises(Crash):
            e.run("r")
    final = engine(flow, tmp_path).run("r")
    assert final.status == "failed" and "used all 2 attempts" in (final.error or "")


def test_resume_with_a_different_definition_is_refused(tmp_path: Path) -> None:
    e = engine(one_step("s"), tmp_path)
    e.start("r", {})
    for other in (
        Workflow("other", "1", (Step("s", lambda c: 1),)),
        Workflow("w", "2", (Step("s", lambda c: 1),)),
        Workflow("w", "1", (Step("s", lambda c: 1), Step("t", lambda c: 1))),
    ):
        with pytest.raises(WorkflowMismatch):
            engine(other, tmp_path).run("r")


def test_two_workers_cannot_run_the_same_run(tmp_path: Path) -> None:
    gate, inside = threading.Event(), threading.Event()

    def slow(ctx: StepContext) -> dict[str, int]:
        inside.set()
        gate.wait(5)
        return {}

    e = Engine(one_step("s", slow), tmp_path, poll_interval=0.01)
    e.start("r", {})
    t = threading.Thread(target=lambda: e.run("r"))
    t.start()
    assert inside.wait(5)
    with pytest.raises(LockError):
        Engine(one_step("s", slow), tmp_path).run("r")
    gate.set()
    t.join()
    assert e.state("r").status == "completed"


def test_crash_after_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[str] = []
    monkeypatch.setattr("incident_agent.durable.engine.kill_self", lambda event: killed.append(event["kind"]))
    hook = crash_after("step_completed:x")
    hook({"kind": "step_completed", "step": "y"})  # wrong step
    hook({"kind": "run_started"})  # wrong event
    assert killed == []
    hook({"kind": "step_completed", "step": "x"})
    crash_after("run_started")({"kind": "run_started"})  # no step given: any step matches
    assert killed == ["step_completed", "run_started"]


# ------------------------------------------------------------------------------------------------ waits and signals


def wait_flow(timeout: float = 30.0) -> Workflow:
    return Workflow("w", "1", (Step("a", lambda c: {"n": 1}), Wait("ok", "approval", timeout), Step("z", lambda c: {})))


def test_wait_without_block_pauses_and_a_signal_releases_it(tmp_path: Path) -> None:
    e = engine(wait_flow(), tmp_path)
    e.start("r", {})
    state = e.run("r", block=False)
    assert state.status == "waiting" and state.waiting_for == "ok" and "z" not in state.results
    e.signal("r", "approval", {"approved": True, "by": "alice"})
    state = e.run("r", block=False)
    assert state.status == "completed" and state.results["ok"] == {
        "timed_out": False,
        "payload": {"approved": True, "by": "alice"},
    }
    assert kinds(e, "r").count("signal_received") == 1
    e.run("r")  # ingesting again never duplicates the signal
    assert kinds(e, "r").count("signal_received") == 1


def test_blocking_wait_polls_until_the_deadline(tmp_path: Path) -> None:
    clock = FakeClock()
    e = engine(wait_flow(timeout=5), tmp_path, clock)
    e.start("r", {})
    state = e.run("r")
    assert state.status == "completed" and state.results["ok"] == {"timed_out": True, "payload": {}}
    assert sum(clock.sleeps) >= 5.0 and "wait_timed_out" in kinds(e, "r")


def test_the_deadline_survives_a_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    e = engine(wait_flow(timeout=100), tmp_path, clock)
    e.start("r", {})
    e.run("r", block=False)
    started = [x for x in e._journal("r").events() if x["kind"] == "wait_started"]  # noqa: SLF001
    assert len(started) == 1
    clock.t += 99  # restart one second before the deadline: not timed out, and the deadline is not extended
    again = engine(wait_flow(timeout=100), tmp_path, clock)
    assert again.run("r", block=False).status == "waiting"
    clock.t += 2
    state = again.run("r", block=False)
    assert state.results["ok"]["timed_out"] is True
    assert kinds(again, "r").count("wait_started") == 1


def test_cancel_between_steps_and_during_a_wait(tmp_path: Path) -> None:
    e = engine(one_step("a"), tmp_path / "x")
    e.start("r", {})
    e.cancel("r")
    assert e.run("r").status == "cancelled" and e.state("r").results == {}
    assert e.run("r").status == "cancelled"
    e2 = engine(wait_flow(), tmp_path / "y")
    e2.start("r", {})
    e2.run("r", block=False)
    e2.cancel("r")
    state = e2.run("r", block=False)
    assert state.status == "cancelled" and state.waiting_for is None and "z" not in state.results


def test_cancel_that_arrives_while_a_blocking_wait_polls(tmp_path: Path) -> None:
    class CancellingClock(FakeClock):
        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            e.cancel("r")

    e = engine(wait_flow(), tmp_path, CancellingClock())
    e.start("r", {})
    state = e.run("r")
    assert state.status == "cancelled" and "z" not in state.results


def test_real_clock_sleeps_and_reads_time() -> None:
    c = Clock()
    before = c.now()
    c.sleep(0.01)
    assert c.now() >= before


# ------------------------------------------------------------------------------------------------ the incident workflow


class FakeBackend:
    """Runs the sandbox child program in a plain subprocess and claims full isolation (tests only)."""

    name = "fake"

    def __init__(self, guarantees: frozenset[str] | None = None) -> None:
        self.guarantees = (
            guarantees
            if guarantees is not None
            else frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS})
        )
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, "fake"

    def run(self, spec: ExecSpec) -> ExecResult:
        self.calls += 1
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-S", "-B", "-c", spec.code], input=spec.stdin, capture_output=True, check=False
        )
        return ExecResult(proc.returncode, proc.stdout, proc.stderr, backend=self.name)


DATA = {
    "meta": {"incident_id": "INC-777", "services": ["checkout-api"]},
    "logs": [
        {"timestamp": "2026-03-01T00:00:00+00:00", "service": "checkout-api", "level": "INFO", "message": "ok"},
        {
            "timestamp": "2026-03-01T00:01:00+00:00",
            "service": "checkout-api",
            "level": "ERROR",
            "message": "container OOMKilled by kubelet",
        },
        {
            "timestamp": "2026-03-01T00:02:00+00:00",
            "service": "checkout-api",
            "level": "ERROR",
            "message": "container OOMKilled by kubelet",
        },
    ],
    "metrics": {},
    "deploys": [],
}


def flow_with(backend: FakeBackend, data: dict[str, Any] | None = None) -> Workflow:
    payload = data if data is not None else DATA
    deps = wf.Deps(backend_factory=lambda: backend, data_loader=lambda _i: payload, approval_timeout=30.0)
    return wf.incident_workflow(deps)


def run_incident(tmp_path: Path, backend: FakeBackend, data: dict[str, Any] | None = None) -> tuple[Engine, Any]:
    e = Engine(flow_with(backend, data), tmp_path, clock=FakeClock(), poll_interval=0.5)
    e.start("r", {"incident_id": "INC-777"})
    return e, e.run("r", block=False)


def test_incident_flow_stops_at_the_approval_and_applies_it_once(tmp_path: Path) -> None:
    backend = FakeBackend()
    e, state = run_incident(tmp_path, backend)
    assert state.status == "waiting" and state.waiting_for == "approval" and backend.calls == 0
    assert state.results["plan_action"]["command_line"] == "restart-service checkout-api --reason oom"
    assert state.results["plan_action"]["verdict"] == "needs_approval"
    e.signal("r", "approval", {"approved": True, "by": "alice"})
    final = e.run("r", block=False)
    ex = final.results["execute"]
    assert final.status == "completed" and ex["mode"] == "applied" and ex["status"] == "ok" and ex["changed"] is True
    assert ex["approved_by"] == "alice" and backend.calls == 1
    assert "restart-service" in final.results["report"]["summary"] and final.results["report"]["changed"] is True
    assert e.run("r").status == "completed" and backend.calls == 1  # resume does not execute again


@pytest.mark.parametrize(
    "payload",
    [
        {"approved": False, "by": "bob"},
        {"approved": True},
        {"approved": True, "by": "  "},
        {"approved": "yes", "by": "x"},
        {},
    ],
)
def test_anything_but_a_clear_named_approval_only_dry_runs(tmp_path: Path, payload: dict[str, Any]) -> None:
    e, _ = run_incident(tmp_path, FakeBackend())
    e.signal("r", "approval", payload)
    ex = e.run("r", block=False).results["execute"]
    assert (
        ex["mode"] == "dry_run"
        and ex["changed"] is False
        and ex["approved_by"] is None
        and "--dry-run" in ex["command_line"]
    )


def test_a_timed_out_approval_only_dry_runs(tmp_path: Path) -> None:
    e, _ = run_incident(tmp_path, FakeBackend())
    e.clock.t += 31  # type: ignore[attr-defined]
    final = e.run("r", block=False)
    assert final.results["approval"]["timed_out"] is True and final.results["execute"]["mode"] == "dry_run"


def test_a_weak_backend_refuses_even_with_an_approval(tmp_path: Path) -> None:
    e, _ = run_incident(tmp_path, FakeBackend(frozenset({ENV, TIME, OUTPUT})))
    e.signal("r", "approval", {"approved": True, "by": "alice"})
    ex = e.run("r", block=False).results["execute"]
    assert ex["status"] == "refused" and ex["changed"] is False and "does not provide" in ex["message"]


def test_a_crash_inside_execute_fails_the_run_instead_of_running_twice(tmp_path: Path) -> None:
    backend = FakeBackend()
    e, _ = run_incident(tmp_path, backend)
    e.signal("r", "approval", {"approved": True, "by": "alice"})

    def hook(event: dict[str, Any]) -> None:
        if event["kind"] == "step_started" and event["step"] == "execute":
            raise Crash

    crashing = Engine(flow_with(backend), tmp_path, clock=FakeClock(), crash_hook=hook)
    with pytest.raises(Crash):
        crashing.run("r", block=False)
    final = Engine(flow_with(backend), tmp_path, clock=FakeClock()).run("r", block=False)
    assert final.status == "failed" and "used all 1 attempts" in (final.error or "") and backend.calls == 0


def test_the_approval_nonce_cannot_be_spent_twice(tmp_path: Path) -> None:
    backend = FakeBackend()
    e, _ = run_incident(tmp_path, backend)
    e.signal("r", "approval", {"approved": True, "by": "alice"})
    assert e.run("r", block=False).results["execute"]["status"] == "ok"
    # replay the execute step by hand with the same run: the deterministic nonce is already consumed in the audit log
    ctx = StepContext("r", "execute", 1, {"incident_id": "INC-777"}, dict(e.state("r").results), e.run_dir("r"))
    again = wf.make_execute(wf.Deps(backend_factory=lambda: backend, data_loader=lambda _i: DATA))(ctx)
    assert again["status"] == "refused" and "already used" in again["message"] and backend.calls == 1


def test_a_timed_out_wait_never_counts_even_with_an_approving_payload(tmp_path: Path) -> None:
    backend = FakeBackend()
    plan = {
        "verdict": "needs_approval",
        "command_line": "restart-service checkout-api",
        "fingerprint": "a" * 20,
        "reason": "",
    }
    results = {"plan_action": plan, "approval": {"timed_out": True, "payload": {"approved": True, "by": "alice"}}}
    ctx = StepContext("r", "execute", 1, {"incident_id": "INC-777"}, results, tmp_path)
    out = wf.make_execute(wf.Deps(backend_factory=lambda: backend, data_loader=lambda _i: DATA))(ctx)
    assert out["mode"] == "dry_run" and out["approved_by"] is None


def test_read_only_plans_need_no_approval(tmp_path: Path) -> None:
    quiet = {**DATA, "logs": [DATA["logs"][0]]}
    e, state = run_incident(tmp_path, FakeBackend(), quiet)
    assert state.status == "completed" and state.skipped == ("approval",)
    assert state.results["diagnose"]["category"] == "unknown"
    assert state.results["plan_action"]["command_line"] == "cluster-status checkout-api"
    assert state.results["execute"]["mode"] == "run" and state.results["execute"]["changed"] is False


def test_denied_plans_fail_without_executing(tmp_path: Path) -> None:
    backend = FakeBackend()
    ctx = StepContext(
        "r", "execute", 1, {"incident_id": "INC-777"}, {"plan_action": {"verdict": "deny", "reason": "no"}}, tmp_path
    )
    with pytest.raises(NonRetryable, match="denied by policy"):
        wf.make_execute(wf.Deps(backend_factory=lambda: backend))(ctx)
    assert backend.calls == 0


def test_triage_and_diagnose_edge_cases(tmp_path: Path) -> None:
    deps = wf.Deps(data_loader=lambda _i: DATA)
    base = StepContext("r", "triage", 1, {}, {}, tmp_path)
    with pytest.raises(NonRetryable, match="incident_id"):
        wf.make_triage(deps)(base)

    def broken(_i: str) -> dict[str, Any]:
        raise FileNotFoundError("nope")

    with pytest.raises(NonRetryable, match="cannot load"):
        wf.make_triage(wf.Deps(data_loader=broken))(
            StepContext("r", "triage", 1, {"incident_id": "INC-9"}, {}, tmp_path)
        )
    no_errors = {"meta": {"services": ["a-svc", 5]}, "logs": [], "deploys": []}
    t = wf.make_triage(wf.Deps(data_loader=lambda _i: no_errors))(
        StepContext("r", "triage", 1, {"incident_id": "INC-9"}, {}, tmp_path)
    )
    assert t["suspect_service"] == "a-svc" and t["services"] == ["a-svc"] and t["error_count"] == 0
    empty = wf.make_triage(wf.Deps(data_loader=lambda _i: {}))(
        StepContext("r", "triage", 1, {"incident_id": "INC-9"}, {}, tmp_path)
    )
    assert empty["suspect_service"] == ""
    # the default loader reads the real dataset
    real = wf.make_triage(wf.Deps())(StepContext("r", "triage", 1, {"incident_id": "INC-002"}, {}, tmp_path))
    assert real["error_count"] > 0 and real["services"]


def test_propose_command_maps_categories_and_rejects_odd_services() -> None:
    p = wf.propose_command
    assert p({"category": "memory", "service": "api"}) == "restart-service api --reason oom"
    assert p({"category": "saturation", "service": "api"}) == "scale-service api --replicas 4"
    assert p({"category": "cache", "service": "api"}) == "flush-cache api"
    assert p({"category": "bad_deploy", "service": "api"}) == "rollback-deploy api"
    assert p({"category": "unknown", "service": "api"}) == "cluster-status api"
    for bad in ("", "Api; rm -rf /", "../x", "a b"):
        assert p({"category": "memory", "service": bad}) == "list-services"


def test_every_dataset_incident_gets_an_allow_listed_plan() -> None:
    from incident_agent.sandbox import evaluate

    deps = wf.Deps()
    for n in range(1, 10):
        incident = f"INC-{n:03d}"
        ctx = StepContext("r", "triage", 1, {"incident_id": incident}, {}, Path("."))
        triage = wf.make_triage(deps)(ctx)
        diagnosis = wf.make_diagnose(deps)(
            StepContext("r", "diagnose", 1, {"incident_id": incident}, {"triage": triage}, Path("."))
        )
        assert evaluate(wf.propose_command(diagnosis), incident).verdict in ("allow", "needs_approval")


def test_workflow_shape_and_helpers(tmp_path: Path) -> None:
    flow = wf.incident_workflow()
    assert tuple(s.name for s in flow.steps) == wf.STEP_ORDER
    execute = next(s for s in flow.steps if s.name == "execute")
    assert isinstance(execute, Step) and execute.retry.max_attempts == 1
    assert wf.needs_approval({}) is False and wf.needs_approval({"plan_action": {"verdict": "needs_approval"}}) is True
    assert (
        wf.deterministic_nonce("r", "execute")
        == wf.deterministic_nonce("r", "execute")
        != wf.deterministic_nonce("s", "execute")
    )
    os.environ.pop("INCIDENT_RUNS_DIR", None)
    assert wf.default_root() == Path(".cache/runs")
    os.environ["INCIDENT_RUNS_DIR"] = str(tmp_path)
    try:
        assert wf.default_root() == tmp_path
    finally:
        del os.environ["INCIDENT_RUNS_DIR"]
    assert wf._text({"msg": "m"}) == "m" and wf._text({}) == ""  # noqa: SLF001


# ------------------------------------------------------------------------------------------------ CLI


@pytest.fixture()
def cli_backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    backend = FakeBackend()
    monkeypatch.setattr("incident_agent.durable.__main__.select_backend", lambda _name=None: backend)
    return backend


def test_cli_full_flow(tmp_path: Path, cli_backend: FakeBackend, capsys: pytest.CaptureFixture[str]) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "list"]) == 0 and "no runs" in capsys.readouterr().out
    assert main([*root, "start", "INC-002", "--run-id", "r1", "--no-wait"]) == 0
    out = capsys.readouterr().out
    assert "waiting for: approval" in out and "step executions" in out
    assert main([*root, "signal", "r1", "approval"]) == 2  # an approval must name who approves
    assert "needs --by" in capsys.readouterr().err
    assert main([*root, "signal", "r1", "approval", "--by", "alice"]) == 0
    assert main([*root, "resume", "r1", "--no-wait"]) == 0
    out = capsys.readouterr().out
    assert "completed" in out and "applied" in out and "restart-service" in out
    assert main([*root, "list"]) == 0 and "r1  completed" in capsys.readouterr().out
    assert main([*root, "status", "r1"]) == 0
    assert cli_backend.calls == 1


def test_cli_reject_cancel_and_failures(
    tmp_path: Path, cli_backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    main([*root, "start", "INC-002", "--run-id", "a", "--no-wait"])
    main([*root, "signal", "a", "approval", "--by", "bob", "--reject"])
    assert main([*root, "resume", "a", "--no-wait"]) == 0
    assert "dry_run" in capsys.readouterr().out
    main([*root, "start", "INC-002", "--run-id", "b", "--no-wait"])
    assert main([*root, "cancel", "b"]) == 0
    assert main([*root, "resume", "b", "--no-wait"]) == 1  # cancelled runs exit non-zero
    assert "cancelled" in capsys.readouterr().out
    assert main([*root, "status", "nope"]) == 2 and "unknown run" in capsys.readouterr().err
    assert main([*root, "start", "INC-999", "--run-id", "c", "--no-wait"]) == 1  # no such incident: the run fails
    assert "failed" in capsys.readouterr().out
    assert main([*root, "resume", "c", "--no-wait", "--retry-failed"]) == 1


def test_cli_demo_kills_a_real_process_and_resumes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path), "demo"]) == 0
    out = capsys.readouterr().out
    assert "worker exit code: " in out and "resumed without repeating any finished step" in out
    assert "'triage': 1, 'diagnose': 1, 'plan_action': 1" in out


def test_cli_demo_without_root_uses_a_temp_dir(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    assert "runs directory:" in capsys.readouterr().out


def test_describe_renders_every_part() -> None:
    from incident_agent.durable.engine import RunState

    state = RunState("r", "failed", {"report": {"summary": "a\nb"}, "x": 1}, ("s",), "boom", None, 3, {})
    text = describe(state, [{"kind": "step_started", "step": "x"}, {"kind": "step_started", "step": "x"}])
    assert "error: boom" in text and "report:\n    a\n    b" in text and "skipped: s" in text and "'x': 2" in text
