"""Stage 16 tests: sandbox execution safety (allow-list, policy, approvals, audit, backends, escape attempts)."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from incident_agent.sandbox import commands, policy
from incident_agent.sandbox import selftest as st
from incident_agent.sandbox.__main__ import main as cli_main
from incident_agent.sandbox.approval import Approval, check_approval, issue_approval
from incident_agent.sandbox.audit import AuditError, AuditLog, one_line
from incident_agent.sandbox.backends import (
    CAPS,
    ENV,
    FILESYSTEM,
    MEMORY,
    NETWORK,
    OUTPUT,
    PROCESSES,
    TIME,
    DockerBackend,
    ExecResult,
    ExecSpec,
    Limits,
    NamespaceBackend,
    ProcessBackend,
    _bind_dirs,
    _docker_env,
    _top_level_links,
    describe_backends,
    docker_run_argv,
    minimal_env,
    namespace_script,
    select_backend,
)
from incident_agent.sandbox.commands import CommandError, Spec
from incident_agent.sandbox.runner import MAX_PAYLOAD, SandboxRunner

LINUX = sys.platform.startswith("linux")
NS = NamespaceBackend()
NS_OK = NS.available()[0]
needs_ns = pytest.mark.skipif(not NS_OK, reason="namespace backend not available here")

DATA: dict[str, Any] = {
    "meta": {"services": ["checkout-api", "orders-db"]},
    "logs": [
        {"timestamp": "2026-03-01T00:00:05+00:00", "service": "checkout-api", "level": "INFO", "message": "ok 55ms"},
        {
            "timestamp": "2026-03-01T00:07:10+00:00",
            "service": "checkout-api",
            "level": "ERROR",
            "message": "Timeout talking to db",
        },
        {"timestamp": "2026-03-01T00:08:10+00:00", "service": "orders-db", "level": "WARN", "message": "slow query"},
    ],
    "metrics": {"cpu_percent": [{"ts": "t1", "value": 10.0}, {"ts": "t2", "value": 30.0}]},
    "deploys": [
        {"timestamp": "2026-02-28T10:00:00", "service": "checkout-api", "version": "1.9.0"},
        {"timestamp": "2026-02-28T12:00:00", "service": "checkout-api", "version": "2.0.0"},
        {"timestamp": "2026-02-28T12:00:00", "service": "orders-db", "version": "n/a"},
    ],
}


def loader(_i: str) -> dict[str, Any]:
    return DATA


class FakeBackend:
    name = "fake"
    guarantees = frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS, MEMORY})

    def __init__(self, result: ExecResult | None = None, guarantees: frozenset[str] | None = None) -> None:
        self.calls: list[ExecSpec] = []
        self.result = result
        if guarantees is not None:
            self.guarantees = guarantees

    def available(self) -> tuple[bool, str]:
        return True, "fake"

    def run(self, spec: ExecSpec) -> ExecResult:
        self.calls.append(spec)
        if self.result is not None:
            return self.result
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-S", "-B", "-c", spec.code], input=spec.stdin, capture_output=True, check=False
        )
        return ExecResult(proc.returncode, proc.stdout, proc.stderr, backend=self.name)


def ok_result(**fields: Any) -> ExecResult:
    return ExecResult(0, json.dumps({"ok": True, "output": "x", **fields}).encode(), b"")


# ---------------------------------------------------------------- commands: parsing


def parse(line: str) -> dict[str, Any]:
    tokens = policy.tokenize(line)
    return commands.parse_tokens(commands.SPECS[tokens[0]], tokens[1:])


def test_parse_valid_commands() -> None:
    assert parse("logs-grep timeout --level error --max 3") == {"pattern": "timeout", "level": "ERROR", "max": 3}
    assert parse("logs-grep 'two words' --service checkout-api")["pattern"] == "two words"
    assert parse("logs-grep x")["max"] == 10
    assert parse("log-stats")["bucket_minutes"] == 5
    assert parse("restart-service checkout-api --reason 'db pool' --dry-run") == {
        "service": "checkout-api",
        "reason": "db pool",
        "dry_run": True,
    }
    assert parse("restart-service checkout-api")["dry_run"] is False
    assert parse("scale-service checkout-api --replicas 5")["replicas"] == 5


@pytest.mark.parametrize(
    "line",
    [
        "logs-grep",
        "logs-grep a b",
        "logs-grep x --max",
        "logs-grep x --max --level",
        "logs-grep x --max=5",
        "logs-grep x --max 5 --max 6",
        "logs-grep x --max 0",
        "logs-grep x --max 51",
        "logs-grep x --max five",
        "logs-grep x --max -3",
        "logs-grep x --level LOUD",
        "logs-grep x --unknown 1",
        "logs-grep -- x",
        "logs-grep -x",
        "logs-grep x --service UPPER",
        "logs-grep " + "a" * 101,
        "cluster-status",
        "cluster-status ../etc",
        "cluster-status a/b",
        "cluster-status " + "a" * 64,
        "scale-service checkout-api",
        "scale-service checkout-api --replicas 21",
        "restart-service checkout-api --dry-run extra",
        "restart-service checkout-api --reason " + "r" * 201,
        "rollback-deploy checkout-api --to ../x",
        "metric-stats CPU",
        "metric-stats",
    ],
)
def test_parse_rejects(line: str) -> None:
    with pytest.raises(CommandError):
        parse(line)


def test_usage_strings() -> None:
    assert commands.SPECS["logs-grep"].usage() == "logs-grep PATTERN [--level LEVEL] [--service SERVICE] [--max MAX]"
    assert commands.SPECS["restart-service"].usage() == "restart-service SERVICE [--reason REASON] [--dry-run]"
    assert commands.SPECS["scale-service"].usage() == "scale-service SERVICE --replicas REPLICAS [--dry-run]"
    assert Spec("x", "y").usage() == "x"
    assert Spec("x", "y", (commands.Arg("opt", "text", positional=True),)).usage() == "x [OPT]"


def test_tiers() -> None:
    for name, spec in commands.SPECS.items():
        if spec.mutating:
            assert spec.tier({"dry_run": False}) == commands.HIGH, name
            assert spec.tier({"dry_run": True}) == commands.LOW, name
            assert spec.requires >= commands.ISOLATED, name
        else:
            assert spec.tier({}) == commands.LOW, name
    assert {n for n, s in commands.SPECS.items() if s.mutating} == {
        "restart-service",
        "scale-service",
        "rollback-deploy",
        "flush-cache",
    }


def test_validate_args_child_side() -> None:
    spec = commands.SPECS["scale-service"]
    assert commands.validate_args(spec, {"service": "a", "replicas": 3}) == {
        "service": "a",
        "replicas": 3,
        "dry_run": False,
    }
    for bad in (
        None,
        [],
        {"service": "a"},
        {"service": "a", "replicas": True},
        {"service": "a", "replicas": 99},
        {"service": "a", "replicas": "x"},
        {"service": 5, "replicas": 2},
        {"service": "a", "replicas": 2, "evil": 1},
        {"service": "a", "replicas": 2, "dry_run": "yes"},
        {"service": "../a", "replicas": 2},
        {"service": "", "replicas": 2},
    ):
        with pytest.raises(CommandError):
            commands.validate_args(spec, bad)


# ---------------------------------------------------------------- commands: handlers


def run_cmd(line: str, apply: bool = False, data: dict[str, Any] | None = None) -> dict[str, Any]:
    tokens = policy.tokenize(line)
    spec = commands.SPECS[tokens[0]]
    args = commands.parse_tokens(spec, tokens[1:])
    return commands.execute(
        {"incident_id": "INC-001", "command": tokens[0], "args": args, "apply": apply, "data": data or DATA}
    )


def test_read_only_handlers() -> None:
    assert "checkout-api  degraded" in run_cmd("list-services")["output"]
    assert "orders-db  healthy" in run_cmd("list-services")["output"]
    assert run_cmd("cluster-status checkout-api")["output"] == "checkout-api: replicas=3 version=2.0.0 health=degraded"
    assert "version=unknown" in run_cmd("cluster-status orders-db")["output"]
    out = run_cmd("logs-grep TIMEOUT")["output"]
    assert out.startswith("1 match(es), showing 1:") and "Timeout talking to db" in out
    assert "no log lines" in run_cmd("logs-grep timeout --level INFO")["output"]
    assert "no log lines" in run_cmd("logs-grep timeout --service orders-db")["output"]
    assert run_cmd("logs-grep o --max 1")["output"].startswith("3 match(es), showing 1:")
    stats = run_cmd("log-stats --bucket-minutes 5")["output"]
    assert "'ERROR': 1" in stats and "2026-03-01T00:05 x2" in stats
    assert run_cmd("metric-stats cpu_percent")["output"] == "cpu_percent: n=2 min=10.00 mean=20.00 max=30.00"
    assert run_cmd("log-stats", data={**DATA, "logs": []})["output"] == "no logs"
    assert run_cmd("list-services", data={**DATA, "meta": {}})["output"] == "no services"


def test_handler_errors_are_clean() -> None:
    assert "unknown service 'nope'" in run_cmd("cluster-status nope")["error"]
    assert "unknown metric 'cpu'" in run_cmd("metric-stats cpu")["error"]
    assert "no values" in run_cmd("metric-stats m", data={**DATA, "metrics": {"m": [{"ts": 1}]}})["error"]
    assert not run_cmd("rollback-deploy orders-db --dry-run")["ok"]
    bad_ts = {**DATA, "logs": [{"timestamp": "garbage", "service": "s", "level": "INFO", "message": "m"}]}
    assert run_cmd("log-stats", data=bad_ts)["ok"]


def test_mutations_dry_run_and_apply() -> None:
    dry = run_cmd("restart-service checkout-api --reason 'pool exhausted' --dry-run", apply=True)
    assert dry["ok"] and dry["changed"] is False and dry["output"].startswith("[dry-run] would restart checkout-api")
    assert "pool exhausted" in dry["output"]
    unapproved = run_cmd("restart-service checkout-api", apply=False)
    assert unapproved["changed"] is False and "[dry-run]" in unapproved["output"]
    done = run_cmd("restart-service checkout-api", apply=True)
    assert (
        done["changed"] is True and "[simulated] did restart" in done["output"] and "no real service" in done["output"]
    )
    assert done["before"]["health"] == "degraded" and done["after"]["health"] == "healthy"
    scale = run_cmd("scale-service checkout-api --replicas 7", apply=True)
    assert scale["after"]["replicas"] == 7 and scale["before"]["replicas"] == 3
    flush = run_cmd("flush-cache checkout-api", apply=True)
    assert flush["after"]["cache"] == "empty"


def test_rollback_rules() -> None:
    auto = run_cmd("rollback-deploy checkout-api", apply=True)
    assert auto["after"]["version"] == "1.9.0" and auto["before"]["version"] == "2.0.0"
    assert run_cmd("rollback-deploy checkout-api --to 1.9.0", apply=True)["ok"]
    assert "not an earlier deployed version" in run_cmd("rollback-deploy checkout-api --to 2.0.0")["error"]
    assert "not an earlier deployed version" in run_cmd("rollback-deploy checkout-api --to 9.9")["error"]
    assert "no earlier version" in run_cmd("rollback-deploy orders-db")["error"]


def test_execute_never_raises() -> None:
    for request in (
        None,
        [],
        {},
        {"command": "rm"},
        {"command": "list-services", "args": {}},
        {"command": "list-services", "args": {}, "data": []},
    ):
        res = commands.execute(request)
        assert res["ok"] is False and "error" in res
    boom = commands.execute(
        {"command": "list-services", "args": {}, "data": {"meta": {"services": [5, None]}, "logs": [5]}}
    )
    assert boom["ok"] is False or boom["ok"] is True
    broken = commands.execute({"command": "log-stats", "args": {"bucket_minutes": 5}, "data": {"logs": [object()]}})
    assert broken == {"ok": False, "error": "internal error: AttributeError"}
    # apply must be literally True, and never for read-only commands or dry runs
    res = commands.execute(
        {"command": "restart-service", "args": {"service": "checkout-api"}, "apply": "yes", "data": DATA}
    )
    assert res["changed"] is False
    res = commands.execute({"command": "list-services", "args": {}, "apply": True, "data": DATA})
    assert res["changed"] is False


def child(payload: bytes) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-B", "-c", Path(commands.__file__).read_text()],
        input=payload,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_child_main_as_a_script() -> None:
    req = {"command": "list-services", "args": {}, "apply": False, "data": DATA}
    assert child(json.dumps(req).encode())["ok"] is True
    assert child(b"not json") == {"ok": False, "error": "input is not JSON"}
    assert child(b"x" * (commands.MAX_STDIN + 1)) == {"ok": False, "error": "input too large"}


def test_child_main_in_process(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class Stdin:
        buffer = io.BytesIO(json.dumps({"command": "list-services", "args": {}, "data": DATA}).encode())

    monkeypatch.setattr(sys, "stdin", Stdin)
    commands._child_main()
    assert json.loads(capsys.readouterr().out)["ok"] is True
    Stdin.buffer = io.BytesIO(b"{")
    commands._child_main()
    assert "not JSON" in capsys.readouterr().out
    Stdin.buffer = io.BytesIO(b"1" * (commands.MAX_STDIN + 5))
    commands._child_main()
    assert "too large" in capsys.readouterr().out


def test_commands_module_is_standard_library_only() -> None:
    source = Path(commands.__file__).read_text()
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "json", "re", "sys", "collections", "dataclasses", "typing"}


# ---------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "a" * 501,
        "list-services\nlist-services",
        "list-services\x00",
        "list-services\x7f",
        "list-services; id",
        "list-services && id",
        "list-services || id",
        "list-services | sh",
        "list-services > f",
        "list-services < f",
        "list-services &",
        "(list-services)",
        "logs-grep $(id)",
        "logs-grep `id`",
        "logs-grep ${HOME}",
        "logs-grep 'open",
        "logs-grep " + " ".join("x" for _ in range(30)),
    ],
)
def test_tokenize_refuses_shell_and_garbage(line: str) -> None:
    with pytest.raises(CommandError):
        policy.tokenize(line)


def test_tokenize_accepts_quoted_operators_and_hash() -> None:
    assert policy.tokenize("logs-grep 'a;b|c'") == ["logs-grep", "a;b|c"]
    assert policy.tokenize("logs-grep a#b") == ["logs-grep", "a#b"]
    assert policy.tokenize('logs-grep "x y"') == ["logs-grep", "x y"]
    with pytest.raises(CommandError):
        policy.tokenize(None)  # type: ignore[arg-type]


def test_evaluate_verdicts() -> None:
    d = policy.evaluate("logs-grep timeout", "INC-001")
    assert (d.verdict, d.tier, d.allowed) == ("allow", "low", True) and d.requires == commands.BASIC
    d = policy.evaluate("restart-service checkout-api", "INC-001")
    assert (d.verdict, d.tier, d.allowed) == ("needs_approval", "high", False)
    assert d.requires == commands.ISOLATED and d.fingerprint and len(d.fingerprint) == 20
    d = policy.evaluate("restart-service checkout-api --dry-run", "INC-001")
    assert (d.verdict, d.tier, d.reason) == (
        "allow",
        "low",
        "dry run: nothing is changed",
    ) and d.requires == commands.BASIC
    d = policy.evaluate("rm -rf /", "INC-001")
    assert d.verdict == "deny" and "not an allow-listed command" in d.reason and not d.allowed
    d = policy.evaluate("LOGS-GREP x", "INC-001")
    assert d.verdict == "deny"
    d = policy.evaluate("logs-grep x --max 99", "INC-001")
    assert d.verdict == "deny" and d.command == "logs-grep"
    assert policy.evaluate("x; y", "INC-001").verdict == "deny"
    assert "z" * 41 not in policy.evaluate("z" * 80, "INC-001").reason  # echoed names are truncated


def test_fingerprint_binds_incident_command_and_args() -> None:
    a = policy.evaluate("restart-service checkout-api", "INC-001").fingerprint
    assert a == policy.evaluate("restart-service  checkout-api", "INC-001").fingerprint  # whitespace is normalised
    assert a != policy.evaluate("restart-service checkout-api", "INC-002").fingerprint
    assert a != policy.evaluate("restart-service orders-db", "INC-001").fingerprint
    assert a != policy.evaluate("restart-service checkout-api --reason x", "INC-001").fingerprint
    assert a != policy.evaluate("flush-cache checkout-api", "INC-001").fingerprint


# ---------------------------------------------------------------- approvals


FP = "0123456789abcdef0123"


def used_none(_n: str) -> bool:
    return False


def test_issue_approval_validation() -> None:
    a = issue_approval(FP, "  alice ", ttl=60, clock=lambda: 100.0)
    assert (a.approved_by, a.issued_at, a.expires_at) == ("alice", 100.0, 160.0) and len(a.nonce) == 32
    assert issue_approval(FP, "a").nonce != issue_approval(FP, "a").nonce
    for args in (("short", "a"), (FP.upper(), "a"), (FP, ""), (FP, "  "), (FP, "x" * 81), (FP, "a\nb")):
        with pytest.raises(ValueError):
            issue_approval(*args)
    for ttl in (0, -1, 3601):
        with pytest.raises(ValueError):
            issue_approval(FP, "a", ttl=ttl)


def test_check_approval_reasons() -> None:
    now = 1000.0
    good = Approval(FP, "alice", now, now + 60, "n")
    assert check_approval(good, FP, now=now, is_used=used_none) is None
    assert check_approval(good, FP, now=now + 59.9, is_used=used_none) is None
    cases = {
        "no approval": (None, FP),
        "not an Approval": ("yes", FP),
        "different command": (good, "f" * 20),
        "does not name": (Approval(FP, " ", now, now + 60, "n"), FP),
        "future": (Approval(FP, "a", now + 61, now + 200, "n"), FP),
        "invalid lifetime": (Approval(FP, "a", now, now, "n"), FP),
        "invalid lifetime ": (Approval(FP, "a", now, now + 3601, "n"), FP),
        "expired": (Approval(FP, "a", now - 100, now - 1, "n"), FP),
        "already used": (Approval(FP, "a", now, now + 60, ""), FP),
    }
    for needle, (approval, fp) in cases.items():
        why = check_approval(approval, fp, now=now, is_used=used_none)  # type: ignore[arg-type]
        assert why and needle.strip() in why, (needle, why)
    assert "expired" in (check_approval(good, FP, now=now + 60, is_used=used_none) or "")
    assert "already used" in (check_approval(good, FP, now=now, is_used=lambda n: n == "n") or "")
    # a small clock skew into the future is tolerated, a larger one is not
    assert check_approval(Approval(FP, "a", now + 59, now + 200, "n"), FP, now=now, is_used=used_none) is None


# ---------------------------------------------------------------- audit


def test_audit_log_redacts_and_persists(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "sub" / "audit.jsonl", clock=lambda: 5.0)
    log.record("execute", command_line="logs-grep token=sk-abcdefghijklmnop", n=3, ok=True)
    log.record("denied", reason="x\n\ny" + "z" * 400)
    rows = log.entries()
    assert rows[0]["ts"] == 5.0 and rows[0]["event"] == "execute" and rows[0]["n"] == 3
    assert "sk-abc" not in json.dumps(rows) and "[redacted]" in rows[0]["command_line"]
    assert "\n" not in rows[1]["reason"] and len(rows[1]["reason"]) <= 300
    (tmp_path / "sub" / "audit.jsonl").write_text(
        (tmp_path / "sub" / "audit.jsonl").read_text() + "garbage\n[1]\n", encoding="utf-8"
    )
    assert len(log.entries()) == 2


def test_audit_nonces_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    first = AuditLog(path)
    assert first.nonce_used("n1") is False
    first.record("approval_consumed", nonce="n1")
    assert first.nonce_used("n1") is True
    assert AuditLog(path).nonce_used("n1") is True and AuditLog(path).nonce_used("n2") is False
    other = AuditLog(None)
    other.record("approval_consumed", nonce="m")
    assert other.nonce_used("m") and not other.nonce_used("x") and other.entries() == []
    path.write_text('{"event": "execute", "nonce": "n3"}\nnot json\n')
    assert AuditLog(path).nonce_used("n3") is False


def test_audit_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AuditError):
        AuditLog(tmp_path).record("x")  # a directory cannot be opened for appending
    path = tmp_path / "a.jsonl"
    path.write_text("")
    log = AuditLog(path)

    def boom(self: Path, *a: Any, **k: Any) -> str:
        raise OSError("nope")

    monkeypatch.setattr(Path, "read_text", boom)
    assert log.nonce_used("anything") is True  # cannot prove unused -> fail closed


def test_one_line() -> None:
    assert one_line("a\tb\x00c") == "a b c"
    assert one_line("Authorization: Bearer abcdefghijkl") == "[redacted]" or "[redacted]" in one_line(
        "Authorization: Bearer abcdefghijkl"
    )
    assert one_line("https://user:pw@host/x") == "[redacted]host/x" or "pw@" not in one_line("https://user:pw@host/x")
    assert one_line("AKIAABCDEFGHIJKLMNOP") == "[redacted]"
    assert one_line("x" * 500, 10) == "x" * 9 + "…"


# ---------------------------------------------------------------- runner


def runner(backend: Any = None, audit: AuditLog | None = None, now: float = 1000.0) -> SandboxRunner:
    return SandboxRunner(backend or FakeBackend(), audit, clock=lambda: now, data_loader=loader)


def approve(r: SandboxRunner, line: str, who: str = "alice", incident: str = "INC-001") -> Approval:
    fp = r.plan(line, incident).fingerprint
    assert fp
    return issue_approval(fp, who, clock=lambda: 1000.0)


def test_runner_read_only_end_to_end() -> None:
    res = runner().run("cluster-status checkout-api", "INC-001")
    assert res.ok and res.output.startswith("checkout-api: replicas=3") and res.backend == "fake" and not res.changed


def test_runner_denies_without_touching_backend() -> None:
    fake = FakeBackend()
    r = runner(fake)
    for line in ("rm -rf /", "list-services; id", "logs-grep $(id)", "", "cluster-status ../x"):
        assert r.run(line, "INC-001").status == "denied"
    for bad in ("../INC-001", "INC-1", "", "INC-001\n", None):
        assert r.run("list-services", bad).status == "denied"  # type: ignore[arg-type]
    assert not fake.calls


def test_runner_needs_approval_and_forged_approvals() -> None:
    fake = FakeBackend()
    r = runner(fake)
    line = "restart-service checkout-api"
    res = r.run(line, "INC-001")
    assert res.status == "needs_approval" and res.decision and res.decision.fingerprint in res.message
    other = issue_approval(
        r.plan("restart-service orders-db", "INC-001").fingerprint or "", "alice", clock=lambda: 1000.0
    )
    expired = Approval(res.decision.fingerprint or "", "alice", 1.0, 2.0, "n")
    for bad in (other, expired, "approved", Approval(res.decision.fingerprint or "", "", 1000.0, 1100.0, "n2")):
        assert r.run(line, "INC-001", bad).status == "refused"  # type: ignore[arg-type]
    assert not fake.calls


def test_runner_approved_run_is_single_use_and_applies() -> None:
    fake = FakeBackend()
    r = runner(fake)
    line = "restart-service checkout-api --reason 'pool'"
    approval = approve(r, line)
    res = r.run(line, "INC-001", approval)
    assert res.ok and res.changed and "[simulated] did restart" in res.output
    assert res.before and res.before["health"] == "degraded" and res.after and res.after["health"] == "healthy"
    assert json.loads(fake.calls[0].stdin)["apply"] is True
    replay = r.run(line, "INC-001", approval)
    assert replay.status == "refused" and "already used" in replay.message and len(fake.calls) == 1
    other_incident = r.run(line, "INC-002", approval)
    assert other_incident.status == "refused" and "different command" in other_incident.message


def test_runner_dry_run_and_read_only_never_apply() -> None:
    fake = FakeBackend()
    r = runner(fake)
    res = r.run("restart-service checkout-api --dry-run", "INC-001")
    assert res.ok and not res.changed and "[dry-run]" in res.output
    r.run("list-services", "INC-001")
    assert all(json.loads(c.stdin)["apply"] is False for c in fake.calls)


def test_runner_weak_backend_is_refused_even_with_approval() -> None:
    weak = FakeBackend(guarantees=commands.BASIC)
    r = runner(weak)
    line = "restart-service checkout-api"
    approval = approve(r, line)
    res = r.run(line, "INC-001", approval)
    assert res.status == "refused" and "does not provide" in res.message and not weak.calls
    # the approval was not burnt by the refusal: it works on a strong backend sharing the same audit log
    strong = SandboxRunner(FakeBackend(), r.audit, clock=lambda: 1000.0, data_loader=loader)
    assert strong.run(line, "INC-001", approval).ok
    assert r.run("list-services", "INC-001").ok  # read-only still works on a weak backend
    assert r.run("restart-service checkout-api --dry-run", "INC-001").ok


def test_runner_audit_trail(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: 7.0)
    r = runner(FakeBackend(), audit)
    line = "restart-service checkout-api"
    r.run("rm -rf /", "INC-001")
    r.run(line, "INC-001")
    r.run(line, "INC-001", approve(r, line, "bob"))
    events = [e["event"] for e in audit.entries()]
    assert events == ["denied", "needs_approval", "approval_consumed", "execute", "result"]
    consumed = next(e for e in audit.entries() if e["event"] == "approval_consumed")
    assert consumed["approved_by"] == "bob" and consumed["incident"] == "INC-001"


def test_runner_unwritable_audit_blocks_execution(tmp_path: Path) -> None:
    fake = FakeBackend()
    (tmp_path / "file").write_text("x")
    r = runner(fake, AuditLog(tmp_path / "file" / "a.jsonl"))  # the parent is a file: every write fails
    line = "restart-service checkout-api"
    res = r.run(line, "INC-001", approve(r, line))
    assert res.status == "refused" and "audit log" in res.message and not fake.calls
    assert r.run("rm -rf /", "INC-001").status == "denied"  # refusals still work without a log
    # an unreadable log cannot prove an approval is unused, so the approval is refused (fail closed)
    r2 = runner(fake, AuditLog(tmp_path))
    assert r2.run(line, "INC-001", approve(r2, line)).status == "refused" and not fake.calls


def test_runner_data_problems() -> None:
    def missing(_i: str) -> dict[str, Any]:
        raise FileNotFoundError

    r = SandboxRunner(FakeBackend(), data_loader=missing)
    res = r.run("list-services", "INC-001")
    assert res.status == "failed" and "FileNotFoundError" in res.message
    huge = SandboxRunner(FakeBackend(), data_loader=lambda _i: {"x": "a" * (MAX_PAYLOAD + 1)})
    assert "too large" in huge.run("list-services", "INC-001").message


@pytest.mark.parametrize(
    ("result", "needle"),
    [
        (ExecResult(None, b"", b"", timed_out=True, killed="timeout"), "stopped the command (timeout)"),
        (ExecResult(None, b"", b"", killed="output-limit"), "output-limit"),
        (ExecResult(3, b"", b"boom"), "exited with code 3"),
        (ExecResult(0, b"not json", b""), "not JSON"),
        (ExecResult(0, b"[1]", b""), "unexpected result"),
        (ExecResult(0, b'{"ok": false, "error": "bad thing"}', b""), "bad thing"),
        (ExecResult(0, b'{"ok": false}', b""), "unknown error"),
    ],
)
def test_runner_child_failures(result: ExecResult, needle: str) -> None:
    res = runner(FakeBackend(result)).run("list-services", "INC-001")
    assert res.status == "failed" and needle in res.message and not res.output


def test_runner_sanitises_child_result() -> None:
    res = runner(FakeBackend(ok_result(output="y" * 9000, changed="yes", before=[1], after="x"))).run(
        "list-services", "INC-001"
    )
    assert (
        res.ok
        and len(res.output) == commands.MAX_OUTPUT
        and res.changed is False
        and res.before is None
        and res.after is None
    )


def test_runner_uses_the_real_commands_source() -> None:
    fake = FakeBackend()
    runner(fake).run("list-services", "INC-001")
    assert fake.calls[0].code == Path(commands.__file__).read_text(encoding="utf-8")
    assert "incident_agent" not in fake.calls[0].code.replace("incident_id", "")


def test_runner_loads_real_incident_data() -> None:
    res = SandboxRunner(ProcessBackend()).run("logs-grep timeout --max 2", "INC-001")
    assert res.ok and res.backend == "process"
    assert SandboxRunner(ProcessBackend()).run("list-services", "INC-001").output.startswith("checkout-api")


# ---------------------------------------------------------------- backends: process + plumbing


def test_limits_validation() -> None:
    for field in (
        "timeout",
        "cpu_seconds",
        "memory_bytes",
        "max_output",
        "max_file_bytes",
        "max_open_files",
        "tmp_bytes",
    ):
        with pytest.raises(ValueError):
            Limits(**{field: 0})
    with pytest.raises(ValueError):
        Limits(max_pids=0)


def test_minimal_env_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    assert "GROQ_API_KEY" not in minimal_env() and set(minimal_env()) >= {"PATH", "LANG"}
    monkeypatch.setenv("DOCKER_HOST", "tcp://x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    env = _docker_env()
    assert env["DOCKER_HOST"] == "tcp://x" and "GROQ_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def test_process_backend_basics() -> None:
    b = ProcessBackend()
    expected = {ENV, TIME, OUTPUT} | ({MEMORY} if os.name != "nt" else set())
    assert b.available()[0] and b.guarantees == expected
    res = b.run(ExecSpec("import sys; print(sys.stdin.read().upper())", b"hello"))
    assert (res.exit_code, res.stdout.strip(), res.backend, res.killed) == (0, b"HELLO", "process", None)
    res = b.run(ExecSpec("import sys; sys.exit(7)"))
    assert res.exit_code == 7
    res = b.run(ExecSpec("raise SystemExit('bye')"))
    assert res.exit_code == 1 and b"bye" in res.stderr


def test_process_backend_scrubs_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_TEST_SECRET", "hunter2")
    res = ProcessBackend().run(ExecSpec("import os; print(sorted(os.environ))"))
    assert b"SBX_TEST_SECRET" not in res.stdout and b"PATH" in res.stdout


def test_process_backend_timeout_and_output_limit() -> None:
    b = ProcessBackend()
    res = b.run(ExecSpec("while True: pass", limits=Limits(timeout=1.0, cpu_seconds=30)))
    assert res.timed_out and res.killed == "timeout" and res.exit_code is None and res.duration < 6
    res = b.run(ExecSpec("print('A' * 10**7)", limits=Limits(timeout=10, max_output=1000)))
    assert res.killed == "output-limit" and res.output_truncated and len(res.stdout) <= 1000
    res = b.run(ExecSpec("import sys; sys.stderr.write('E' * 5000)", limits=Limits(max_output=100)))
    assert len(res.stderr) <= 100


@pytest.mark.skipif(not LINUX, reason="rlimits are POSIX only")
def test_process_backend_resource_limits() -> None:
    res = ProcessBackend().run(
        ExecSpec("x = bytearray(b'1') * (3 * 1024**3)", limits=Limits(memory_bytes=256 * 1024 * 1024))
    )
    assert res.exit_code != 0 and b"MemoryError" in res.stderr
    res = ProcessBackend().run(
        ExecSpec("open('/tmp/sbx-big', 'wb').write(b'x' * (4 * 1024 * 1024))", limits=Limits(max_file_bytes=1024))
    )
    assert res.exit_code != 0
    Path("/tmp/sbx-big").unlink(missing_ok=True)


def test_process_backend_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    res = ProcessBackend().run(ExecSpec("print(1)"))
    assert res.killed == "unavailable" and res.exit_code is None and b"cannot start" in res.stderr


@pytest.mark.skipif(not LINUX, reason="process groups are POSIX only")
def test_process_backend_kills_same_group_stragglers() -> None:
    marker = "sbx-straggler-" + os.urandom(6).hex()
    child_code = f"import time\\n# {marker}\\ntime.sleep(30)"
    code = f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', '{child_code}'])\nprint('done')"
    res = ProcessBackend().run(ExecSpec(code))
    assert res.exit_code == 0 and b"done" in res.stdout
    import time

    time.sleep(0.3)
    assert st.survivors(marker) == []


# ---------------------------------------------------------------- backends: namespace


def test_namespace_script_contents() -> None:
    lim = Limits(tmp_bytes=16 * 1024 * 1024)
    script = namespace_script(
        ["/usr", "/lib"], "/usr/bin/setpriv", "/usr/bin/python3.13", lim, [("lib64", "usr/lib64")]
    )
    lines = script.splitlines()
    assert lines[0] == "set -e" and "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in lines[1]
    assert 'mount --bind /usr "$root"/usr' in script and 'mount -o remount,bind,ro,nosuid,nodev "$root"/usr' in script
    assert 'ln -s usr/lib64 "$root/lib64"' in script
    assert 'noexec,nosuid,nodev tmpfs "$root/tmp"' in script and "size=16m" in script
    assert script.index('mount -o remount,ro "$root"') < script.index("pivot_root . old")
    assert script.index("pivot_root . old") < script.index("umount2")
    assert (
        script.rstrip().endswith('--no-new-privs "$@"')
        and "--bounding-set=-all" in script
        and "--ambient-caps=-all" in script
    )
    assert "mount --bind /usr/bin/setpriv" not in script  # already covered by the /usr bind
    other = namespace_script(["/opt/py"], "/usr/bin/setpriv", "/opt/py/bin/python", lim, [])
    assert "mount --bind /usr/bin/setpriv" in other
    assert 'mount --bind /dev/null "$root/dev/null"' in other


def test_bind_dirs_are_not_nested() -> None:
    dirs = _bind_dirs()
    assert dirs and all(not (a != b and (a + "/").startswith(b + "/")) for a in dirs for b in dirs)
    for name, target in _top_level_links():
        assert name in {"bin", "sbin", "lib", "lib32", "lib64"} and target


@needs_ns
def test_namespace_backend_runs_programs() -> None:
    assert NS.available() == (True, "ok (probe ran)")
    res = NS.run(ExecSpec("import sys; print(sys.stdin.read()[::-1])", b"abc"))
    assert res.exit_code == 0 and res.stdout.strip() == b"cba" and res.backend == "namespace"


def test_namespace_backend_unavailable_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    b = NamespaceBackend()
    monkeypatch.setattr(sys, "platform", "win32")
    assert b.available() == (False, "needs Linux")
    b = NamespaceBackend()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    assert "needs unshare" in b.available()[1]
    b = NamespaceBackend()
    monkeypatch.setattr("shutil.which", lambda *a, **k: "/bin/true")
    monkeypatch.setattr(NamespaceBackend, "run", lambda self, spec: ExecResult(1, b"", b"denied"))
    assert b.available() == (False, "user namespaces are not permitted here")
    assert b.available() == (False, "user namespaces are not permitted here")  # cached


# ---------------------------------------------------------------- backends: docker


def test_docker_argv_has_every_isolation_flag() -> None:
    spec = ExecSpec("print(1)", b"", Limits(memory_bytes=512 * 1024 * 1024, max_pids=33, cpu_seconds=4))
    argv = docker_run_argv("docker", "sbx-1", "img:1", spec)
    joined = " ".join(argv)
    for flag in (
        "--network none",
        "--ipc none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 33",
        "--memory 512m",
        "--memory-swap 512m",
        "--user 65534:65534",
        "--pull never",
        "--ulimit cpu=4:4",
        "--ulimit core=0:0",
        "--rm",
        "--name sbx-1",
    ):
        assert flag in joined, flag
    assert argv[-7:] == ["img:1", "python", "-I", "-S", "-B", "-c", "print(1)"]
    assert "-v" not in argv and "--volume" not in argv and "--privileged" not in argv and "--network host" not in joined
    assert "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m" in joined


def fake_docker(tmp_path: Path, mode: str = "ok") -> Path:
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(
        f"""#!{sys.executable}
import os, subprocess, sys
a = sys.argv[1:]
with open({str(log)!r}, "a") as fh:
    fh.write(" ".join(a[:3]) + "\\n")
mode = {mode!r}
if a[0] == "info":
    sys.exit(1 if mode == "daemon-down" else 0)
if a[0] == "image":
    sys.exit(1 if mode == "no-image" else 0)
if a[0] == "rm":
    sys.exit(0)
if a[0] == "run":
    code = a[-1]
    sys.exit(subprocess.run([{sys.executable!r}, "-I", "-S", "-B", "-c", code], stdin=sys.stdin).returncode)
sys.exit(2)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.skipif(os.name == "nt", reason="fake docker is a POSIX script")
def test_docker_backend_lifecycle_with_a_fake_cli(tmp_path: Path) -> None:
    b = DockerBackend(str(fake_docker(tmp_path)), "img:1")
    assert b.available() == (True, "ok (image img:1)") and b.available()[0]
    res = b.run(ExecSpec("import sys; print(sys.stdin.read())", b"hi"))
    assert res.exit_code == 0 and res.stdout.strip() == b"hi" and res.backend == "docker"
    log = (tmp_path / "docker.log").read_text()
    assert "run --rm -i" in log and "rm -f sbx-" in log  # the container is always cleaned up
    slow = b.run(ExecSpec("while True: pass", limits=Limits(timeout=1.0)))
    assert slow.timed_out
    assert (tmp_path / "docker.log").read_text().count("rm -f sbx-") >= 3  # killed runs are removed explicitly


@pytest.mark.skipif(os.name == "nt", reason="fake docker is a POSIX script")
@pytest.mark.parametrize(
    ("mode", "needle"), [("daemon-down", "daemon is not reachable"), ("no-image", "docker pull img:1")]
)
def test_docker_backend_unavailable(tmp_path: Path, mode: str, needle: str) -> None:
    ok, why = DockerBackend(str(fake_docker(tmp_path, mode)), "img:1").available()
    assert not ok and needle in why


def test_docker_backend_missing_or_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    missing = DockerBackend()
    assert missing.available() == (False, "docker is not installed")
    res = missing.run(ExecSpec("print(1)"))
    assert res.killed == "unavailable" and b"docker missing" in res.stderr
    broken = DockerBackend(str(tmp_path / "nope"), "img")
    assert "docker did not answer" in broken.available()[1]
    monkeypatch.setenv("SANDBOX_DOCKER_IMAGE", "custom:2")
    assert DockerBackend("d")._image == "custom:2"


# ---------------------------------------------------------------- backend selection


class Stub:
    def __init__(self, name: str, ok: bool) -> None:
        self.name, self._ok, self.guarantees = name, ok, frozenset({ENV})

    def available(self) -> tuple[bool, str]:
        return self._ok, "why"

    def run(self, spec: ExecSpec) -> ExecResult:  # pragma: no cover - never run
        raise AssertionError


def test_select_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    pool: dict[str, Any] = {
        "docker": Stub("docker", False),
        "namespace": Stub("namespace", True),
        "process": Stub("process", True),
    }
    assert select_backend(None, pool).name == "namespace"
    pool["docker"] = Stub("docker", True)
    assert select_backend(None, pool).name == "docker"
    assert select_backend("process", pool).name == "process"
    monkeypatch.setenv("SANDBOX_BACKEND", "namespace")
    assert select_backend(None, pool).name == "namespace"
    assert select_backend("docker", pool).name == "docker"  # explicit argument beats the environment
    with pytest.raises(RuntimeError, match="unknown sandbox backend"):
        select_backend("vm", pool)
    pool["docker"] = Stub("docker", False)
    with pytest.raises(RuntimeError, match="not available: why"):
        select_backend("docker", pool)
    monkeypatch.delenv("SANDBOX_BACKEND")
    with pytest.raises(RuntimeError, match="no sandbox backend"):
        select_backend(None, {"process": Stub("process", False)})
    rows = describe_backends(pool)
    assert len(rows) == 3 and "unavailable" in rows[0] and "available" in rows[1]
    assert select_backend().name in {"docker", "namespace", "process"}


# ---------------------------------------------------------------- the escape suite


def statuses(checks: list[st.Check]) -> dict[str, str]:
    return {c.name: c.status for c in checks}


def test_attack_catalogue_covers_the_roadmap_requirements() -> None:
    cats = {a.category for a in st.ATTACKS}
    assert cats >= {"env", "network", "filesystem", "caps", "processes", "resources"}
    claims = {a.claim for a in st.ATTACKS}
    assert claims >= {ENV, NETWORK, FILESYSTEM, CAPS, PROCESSES, MEMORY}
    assert len({a.name for a in st.ATTACKS}) == len(st.ATTACKS)


@needs_ns
def test_namespace_backend_holds_every_escape_attempt() -> None:
    checks = st.run_selftest(NS)
    assert not st.has_failures(checks), st.format_report(checks)
    assert all(c.status == st.PASS for c in checks), st.format_report(checks)
    assert len([c for c in checks if c.category != "policy"]) >= 13


@pytest.mark.skipif(not LINUX, reason="positive controls need Linux")
def test_the_attacks_really_escape_a_weak_backend() -> None:
    """Positive control: on the process backend the same attacks succeed, so a PASS elsewhere is meaningful."""
    checks = st.run_attacks(ProcessBackend())
    by_name = {c.name: c for c in checks}
    for name in (
        "no network (loopback, internet, DNS)",
        "host files are invisible",
        "symlink cannot reach host files",
        "cannot write outside the scratch area",
        "detached daemon dies with the sandbox",
    ):
        assert by_name[name].status == st.SKIP and "would escape" in by_name[name].detail, name
    assert by_name["environment holds no host secrets"].status == st.PASS
    assert not st.has_failures(checks)


def test_selftest_flags_a_leaky_backend_that_claims_isolation() -> None:
    class Liar(ProcessBackend):
        name = "liar"
        guarantees = frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS, MEMORY})

    checks = st.run_attacks(Liar())
    assert st.has_failures(checks)
    failed = {c.name for c in checks if c.status == st.FAIL}
    assert {"no network (loopback, internet, DNS)", "host files are invisible"} <= failed
    assert all(c.detail.startswith("ESCAPE") for c in checks if c.status == st.FAIL and c.category != "control")


def test_selftest_unavailable_backend_is_skipped() -> None:
    checks = st.run_selftest(Stub("ghost", False))  # type: ignore[arg-type]
    assert len(checks) == 1 and checks[0].status == st.SKIP and not st.has_failures(checks)


def test_memory_attack_is_not_run_without_a_memory_guarantee() -> None:
    backend = FakeBackend(guarantees=frozenset({ENV, TIME, OUTPUT}))
    attack = next(a for a in st.ATTACKS if a.claim == MEMORY)
    check = st._one_attack(backend, attack, {}, Limits(), {})
    assert check.status == st.SKIP and "unbounded allocation" in check.detail
    assert not backend.calls


def test_selftest_restores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_SELFTEST_SECRET", raising=False)
    st.run_attacks(FakeBackend(ExecResult(0, b'{"escaped": false, "detail": "x"}', b"")))
    assert "SANDBOX_SELFTEST_SECRET" not in os.environ
    monkeypatch.setenv("SANDBOX_SELFTEST_SECRET", "keep")
    st.run_attacks(FakeBackend(ExecResult(0, b'{"escaped": false, "detail": "x"}', b"")))
    assert os.environ["SANDBOX_SELFTEST_SECRET"] == "keep"


def test_verdict_and_parse_helpers() -> None:
    strong, weak = FakeBackend(), FakeBackend(guarantees=frozenset({ENV}))
    assert st._verdict(strong, NETWORK, False, "d")[0] == st.PASS
    assert st._verdict(strong, NETWORK, True, "d") == (st.FAIL, "ESCAPE: d")
    assert (
        st._verdict(weak, NETWORK, True, "d")[0] == st.SKIP
        and "would escape" in st._verdict(weak, NETWORK, True, "d")[1]
    )
    assert "held anyway" in st._verdict(weak, NETWORK, False, "d")[1]
    assert st._verdict(strong, NETWORK, None, "d")[0] == st.SKIP
    assert st._parse(ExecResult(0, b'noise\n{"escaped": true}\n', b"")) == {"escaped": True}
    assert st._parse(ExecResult(0, b"", b"")) is None and st._parse(ExecResult(0, b"[1]", b"")) is None
    assert st._parse(ExecResult(0, b"nope", b"")) is None


def test_attack_without_report_and_killed_memory_bomb() -> None:
    silent = FakeBackend(ExecResult(1, b"", b"crash"))
    checks = {c.name: c for c in st.run_attacks(silent)}
    assert (
        checks["host files are invisible"].status == st.FAIL
        and "did not report" in checks["host files are invisible"].detail
    )
    assert checks["memory bomb is refused or killed"].status == st.PASS  # being killed counts as held
    unclaimed = FakeBackend(ExecResult(1, b"", b"crash"), guarantees=frozenset({ENV}))
    assert {c.name: c for c in st.run_attacks(unclaimed)}["host files are invisible"].status == st.SKIP


def test_policy_checks_pass_and_detect_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = st.policy_checks()
    assert [c.status for c in checks] == [st.PASS] * 3 and len(st.HOSTILE_LINES) >= 30
    # break the policy on purpose: the suite must notice
    monkeypatch.setattr(policy, "tokenize", lambda line: ["cluster-status", "checkout-api"])
    monkeypatch.setattr("incident_agent.sandbox.runner.evaluate", policy.evaluate)
    broken = st.policy_checks()
    assert st.has_failures(broken) and broken[0].status == st.FAIL


def test_format_report() -> None:
    text = st.format_report([st.Check("a", "n1", st.PASS, "d"), st.Check("b", "n2", st.SKIP, "e")])
    assert "PASS  a" in text and "1 passed, 0 failed, 1 skipped" in text


def test_recording_backend_counts_calls() -> None:
    rb = st.RecordingBackend()
    assert rb.available()[0] and rb.run(ExecSpec("x")).exit_code == 0 and rb.calls == 1


# ---------------------------------------------------------------- CLI


def test_cli_commands_and_backends(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["commands"]) == 0
    out = capsys.readouterr().out
    assert "HIGH  restart-service" in out and "low   list-services" in out
    assert cli_main(["backends"]) == 0
    assert "process" in capsys.readouterr().out


def test_cli_run_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    audit = str(tmp_path / "a.jsonl")
    base = ["run", "INC-001", "", "--backend", "process", "--audit", audit]

    def go(line: str, *extra: str) -> tuple[int, str]:
        argv = [*base[:2], line, *base[3:], *extra]
        code = cli_main(argv)
        return code, capsys.readouterr().out

    code, out = go("logs-grep timeout --max 2")
    assert code == 0 and out.startswith("[ok] backend=process")
    code, out = go("rm -rf /")
    assert code == 1 and "[denied]" in out
    code, out = go("restart-service checkout-api")
    assert code == 3 and "fingerprint" in out
    fp = out.split("fingerprint ")[1].split()[0]
    code, out = go("restart-service checkout-api", "--approve", fp)
    assert code == 2
    code, out = go("restart-service checkout-api", "--approve", "bad", "--approved-by", "me")
    assert code == 2
    code, out = go("restart-service checkout-api", "--approve", fp, "--approved-by", "me")
    assert code == 1 and "does not provide" in out  # the process backend cannot run a mutating command
    assert (tmp_path / "a.jsonl").exists()
    monkeypatch.setenv("SANDBOX_BACKEND", "nonexistent")
    assert cli_main(["run", "INC-001", "list-services"]) == 2


@needs_ns
def test_cli_approved_remediation_in_the_namespace_sandbox(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audit = str(tmp_path / "a.jsonl")
    args = ["run", "INC-001", "scale-service checkout-api --replicas 4", "--backend", "namespace", "--audit", audit]
    assert cli_main(args) == 3
    fp = capsys.readouterr().out.split("fingerprint ")[1].split()[0]
    assert cli_main([*args, "--approve", fp, "--approved-by", "hasan"]) == 0
    out = capsys.readouterr().out
    assert "[ok] backend=namespace" in out and "[simulated] did scale checkout-api from 3 to 4 replicas" in out
    capsys.readouterr()
    # every CLI call is a fresh human approval; the log records each one with its own nonce
    assert cli_main([*args, "--approve", fp, "--approved-by", "hasan"]) == 0
    consumed = [e for e in AuditLog(audit).entries() if e["event"] == "approval_consumed"]
    assert len(consumed) == 2 and consumed[0]["nonce"] != consumed[1]["nonce"]


def test_cli_selftest(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_agent.sandbox.__main__.run_selftest", lambda b: [st.Check("x", "n", st.PASS, "d")])
    assert cli_main(["selftest", "--backend", "process"]) == 0
    assert "backend: process" in capsys.readouterr().out
    monkeypatch.setattr("incident_agent.sandbox.__main__.run_selftest", lambda b: [st.Check("x", "n", st.FAIL, "d")])
    assert cli_main(["selftest", "--backend", "process"]) == 1


# ---------------------------------------------------------------- roadmap and risk-spec conformance


ROOT = Path(__file__).resolve().parent.parent


def test_conforms_to_the_risk_classification() -> None:
    spec = (ROOT / "openspec" / "risk-classification.md").read_text()
    high = spec.split("## High risk")[1]
    for phrase in ("remediation script or shell command", "Restarting, scaling", "Rolling back a deploy"):
        assert phrase in high
    mutating = {n for n, s in commands.SPECS.items() if s.mutating}
    assert mutating == {"restart-service", "scale-service", "rollback-deploy", "flush-cache"}
    for (
        name
    ) in mutating:  # every mutating command is high risk without --dry-run, needs approval, never runs unisolated
        d = policy.evaluate(f"{name} checkout-api" + (" --replicas 2" if name == "scale-service" else ""), "INC-001")
        assert d.verdict == "needs_approval" and d.tier == "high" and d.requires >= commands.ISOLATED, name


def test_roadmap_stage_16_is_what_this_stage_builds() -> None:
    roadmap = (ROOT / "ROADMAP.md").read_text()
    stage = roadmap.split("**Stage 16")[1].split("##")[0]
    assert "isolated environment with an allow-list" in stage and "sandbox-escape attempt tests" in stage
    assert policy.evaluate("rm -rf /", "INC-001").verdict == "deny"  # allow-list
    assert st.ATTACKS and st.HOSTILE_LINES  # escape-attempt tests exist


def test_equals_form_gets_a_helpful_message() -> None:
    with pytest.raises(CommandError, match="write options as '--name value'"):
        parse("logs-grep x --max=5")


def test_child_source_fits_a_windows_command_line() -> None:
    """The source travels as a ``-c`` argument; CreateProcess allows about 32 000 characters."""
    assert len(Path(commands.__file__).read_text(encoding="utf-8")) < 24_000
