"""Stage 18: approval gate, escalation, authentication and the bridge to durable runs."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from incident_agent.durable import workflows as wf
from incident_agent.durable.engine import Engine
from incident_agent.durable.journal import LockError, RunLock
from incident_agent.hitl import (
    DEFAULT_POLICY,
    ApprovalGate,
    AuthError,
    BadConfirmation,
    CallbackNotifier,
    Controller,
    DuplicateApproval,
    EscalationPolicy,
    Level,
    MultiNotifier,
    NotEligible,
    Notification,
    NotifyResult,
    NotPending,
    OutboxNotifier,
    PolicyError,
    RequestStore,
    Roster,
    SelfApproval,
    SlackNotifier,
    approval_payload,
    load_policy,
    request_id_for,
)
from incident_agent.hitl import __main__ as cli
from incident_agent.hitl.notify import build_notification
from incident_agent.hitl.store import check_request_id, fold
from incident_agent.reporting.slack import SendResult
from tests.test_durable import DATA, FakeBackend

FP = "0123456789abcdef0123"
CONFIRM = FP[:8]


class Now:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def policy(required: int = 1, timeout: float = 100.0) -> EscalationPolicy:
    return EscalationPolicy(
        (
            Level("primary", ("oncall",), timeout),
            Level("secondary", ("lead",), timeout, mention="<!here>"),
            Level("management", ("manager",), timeout),
        ),
        {"high": required},
    )


class World:
    """A gate with three levels, five approvers and a recording notifier."""

    def __init__(self, tmp: Path, *, required: int = 1, timeout: float = 100.0, fail: bool = False) -> None:
        self.now = Now()
        self.notes: list[Notification] = []
        self.fail = fail

        def record(n: Notification) -> bool:
            self.notes.append(n)
            return not self.fail

        self.roster = Roster(tmp / "roster.json")
        self.tokens = {
            name: self.roster.add(name, [role])
            for name, role in (
                ("alice", "oncall"),
                ("dave", "oncall"),
                ("bob", "lead"),
                ("carol", "manager"),
                ("zed", "intern"),
            )
        }
        self.store = RequestStore(tmp / "requests", self.now)
        self.gate = ApprovalGate(self.store, self.roster, policy(required, timeout), CallbackNotifier(record), self.now)

    def open(self, rid: str = "r1.approval", **over: Any) -> Any:
        args: dict[str, Any] = {
            "run_id": "r1",
            "incident_id": "INC-777",
            "command_line": "restart-service checkout-api --reason oom",
            "fingerprint": FP,
            "tier": "high",
            "reason": "OOMKilled",
        }
        args.update(over)
        return self.gate.open(rid, **args)

    def approve(self, who: str, rid: str = "r1.approval", **kw: Any) -> Any:
        kw.setdefault("confirm", CONFIRM)
        return self.gate.approve(rid, who, self.tokens[who], **kw)

    def reject(self, who: str, rid: str = "r1.approval", **kw: Any) -> Any:
        return self.gate.reject(rid, who, self.tokens[who], **kw)

    def state(self, rid: str = "r1.approval") -> Any:
        return self.store.state(rid)

    def kinds(self, rid: str = "r1.approval") -> list[str]:
        return [e["kind"] for e in self.store.journal(rid).events()]


@pytest.fixture()
def w(tmp_path: Path) -> World:
    return World(tmp_path)


# ------------------------------------------------------------------------------------------------ policy


def test_policy_validation() -> None:
    lv = Level("a", ("oncall",), 10)
    for bad in (
        lambda: Level("A", ("x",), 1),
        lambda: Level("a", (), 1),
        lambda: Level("a", ("Bad Role",), 1),
        lambda: Level("a", ("x",), 0),
        lambda: Level("a", ("x",), 8 * 24 * 3600),
        lambda: Level("a", ("x",), "5"),  # type: ignore[arg-type]
        lambda: Level("a", ("x",), True),
        lambda: EscalationPolicy(()),
        lambda: EscalationPolicy((lv,) * 6),
        lambda: EscalationPolicy((lv, lv)),
        lambda: EscalationPolicy((lv,), {"high": 0}),
        lambda: EscalationPolicy((lv,), {"high": 4}),
        lambda: EscalationPolicy((lv,), {"high": True}),  # type: ignore[dict-item]
        lambda: EscalationPolicy((lv,), default_required=0),
    ):
        with pytest.raises(PolicyError):
            bad()


def test_policy_numbers_and_json_roundtrip() -> None:
    p = policy(2, 50.0)
    assert p.total_timeout == 150.0 and p.required_for("high") == 2 and p.required_for("medium") == 1
    assert p.required_for(None) == 1
    assert p.deadlines(1000.0) == [1050.0, 1100.0, 1150.0]
    assert p.deadlines(1000.0, 1075.0) == [1050.0, 1075.0, 1075.0]  # the run's own deadline caps every level
    assert p.roles_up_to(0) == {"oncall"} and p.roles_up_to(1) == {"oncall", "lead"}
    again = EscalationPolicy.from_json(json.loads(json.dumps(p.to_json())))
    assert again == p and DEFAULT_POLICY.total_timeout == 1200.0


def test_policy_from_json_and_files(tmp_path: Path) -> None:
    for bad in ([], {}, {"levels": "x"}, {"levels": [5]}, {"levels": [{"name": "a"}]}, {"levels": [{"roles": ["x"]}]}):
        with pytest.raises(PolicyError):
            EscalationPolicy.from_json(bad)
    with pytest.raises(PolicyError, match="'required'"):
        EscalationPolicy.from_json({"levels": [{"name": "a", "roles": ["x"], "timeout": 5}], "required": 3})
    good = tmp_path / "p.json"
    good.write_text(json.dumps({"levels": [{"name": "a", "roles": ["x"], "timeout": 5}]}))
    assert load_policy(good).levels[0].name == "a" and load_policy(None) is DEFAULT_POLICY
    with pytest.raises(PolicyError, match="does not exist"):
        load_policy(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(PolicyError, match="cannot be read"):
        load_policy(tmp_path / "bad.json")


# ------------------------------------------------------------------------------------------------ roster


def test_roster_add_authenticate_rekey_remove(tmp_path: Path) -> None:
    r = Roster(tmp_path / "x" / "roster.json")
    assert r.names() == {}
    t1 = r.add("alice", ["oncall", "oncall", "lead"])
    assert r.authenticate("alice", t1).roles == {"oncall", "lead"} and r.names() == {"alice": ["lead", "oncall"]}
    assert oct((tmp_path / "x" / "roster.json").stat().st_mode & 0o777) == "0o600" or os.name == "nt"
    assert t1 not in (tmp_path / "x" / "roster.json").read_text()  # only a digest is stored
    t2 = r.add("alice", ["oncall"])
    assert t1 != t2
    with pytest.raises(AuthError):
        r.authenticate("alice", t1)  # re-keying kills the old token
    assert r.authenticate("alice", t2).roles == {"oncall"}
    assert r.remove("alice") is True and r.remove("alice") is False
    with pytest.raises(AuthError):
        r.authenticate("alice", t2)


def test_roster_rejects_bad_input_and_bad_files(tmp_path: Path) -> None:
    r = Roster(tmp_path / "roster.json")
    for name, roles in (
        ("Alice", ["a"]),
        ("", ["a"]),
        ("a b", ["a"]),
        ("x" * 25, ["a"]),
        ("ok", []),
        ("ok", ["Bad Role"]),
    ):
        with pytest.raises(ValueError):
            r.add(name, roles)
    for bad_token in ("", "nope", None):
        with pytest.raises(AuthError, match="unknown approver or wrong token"):
            r.authenticate("ghost", bad_token)  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        r.authenticate(5, "x")  # type: ignore[arg-type]
    (tmp_path / "roster.json").write_text("not json")
    with pytest.raises(AuthError, match="cannot be read"):
        r.names()
    (tmp_path / "roster.json").write_text("[1]")
    with pytest.raises(AuthError, match="not an object"):
        r.names()
    # an entry without a digest or with odd roles can never authenticate
    (tmp_path / "roster.json").write_text(
        json.dumps({"a": {"roles": ["x"]}, "b": {"token_sha256": "0" * 64, "roles": "x"}, "c": 5})
    )
    for n in ("a", "b", "c"):
        with pytest.raises(AuthError):
            r.authenticate(n, "0" * 64)


def test_roster_save_failure_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Roster(tmp_path / "roster.json")

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("incident_agent.hitl.roster.os.replace", boom)
    with pytest.raises(OSError):
        r.add("alice", ["oncall"])
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------------------------------------ store


def test_request_ids_and_store_basics(tmp_path: Path) -> None:
    for bad in ("", "../x", "a/b", "-x", "x" * 81, "end.", 5):
        with pytest.raises(ValueError):
            check_request_id(bad)  # type: ignore[arg-type]
    s = RequestStore(tmp_path / "none")
    assert s.ids() == [] and not s.exists("a")
    with pytest.raises(KeyError):
        s.state("a")
    with pytest.raises(ValueError, match="no opening event"):
        fold("a", [])
    with pytest.raises(ValueError, match="no opening event"):
        fold("a", [{"kind": "approved"}])


def test_store_lock_waits_then_gives_up(tmp_path: Path) -> None:
    s = RequestStore(tmp_path)
    holder = RunLock(s.dir("r"))
    holder.acquire()
    try:
        with pytest.raises(LockError, match="busy"), s.locked("r", wait=0.1):
            pass
    finally:
        holder.release()
    with s.locked("r", wait=0.1):
        pass


# ------------------------------------------------------------------------------------------------ opening


def test_open_validates_and_is_idempotent(w: World) -> None:
    for bad in (
        {"tier": "low"},
        {"fingerprint": "xyz"},
        {"fingerprint": "A" * 20},
        {"command_line": " "},
        {"command_line": "x" * 501},
    ):
        with pytest.raises(ValueError):
            w.open(**bad)
    with pytest.raises(ValueError, match="already passed"):
        w.open(hard_deadline=w.now.t - 1)
    st = w.open()
    assert st.pending and st.level == 0 and st.required == 1 and st.requested_by is None
    again = w.open()
    assert again.events == st.events and w.kinds().count("request_opened") == 1
    with pytest.raises(ValueError, match="different action"):
        w.open(fingerprint="f" * 20)
    with pytest.raises(ValueError):
        w.open("../evil")


def test_open_notifies_the_first_level(w: World) -> None:
    w.open(reason="secret=sk-abcdefghijklmnopqrstuvwx and more")
    assert len(w.notes) == 1
    n = w.notes[0]
    assert n.kind == "opened" and n.level_name == "primary" and n.roles == ("oncall",)
    assert "restart-service checkout-api" in n.text and f"--confirm {CONFIRM}" in n.text
    assert "sk-abcdefghijklmnopqrstuvwx" not in n.text and "never approves" in n.text


# ------------------------------------------------------------------------------------------------ the roadmap scenario


def test_pending_approval_then_timeout_escalation_never_approves(w: World) -> None:
    """ROADMAP Stage 18 output: a pending approval and a timeout escalation. Silence must end in a refusal."""
    w.open()
    assert w.state().status == "pending" and w.gate.tick() == []  # nothing happens before the deadline
    w.now.t += 101
    assert [x["level"] for x in w.gate.tick()] == ["secondary"] and w.state().status == "pending"
    w.now.t += 100
    assert [x["level"] for x in w.gate.tick()] == ["management"] and w.state().status == "pending"
    w.now.t += 100
    out = w.gate.tick()
    st = w.state()
    assert out[0]["status"] == "expired" and st.status == "expired" and st.outcome != "approved" and st.approvals == ()
    assert [(n.kind, n.level_name) for n in w.notes] == [
        ("opened", "primary"),
        ("escalated", "secondary"),
        ("escalated", "management"),
        ("resolved", "management"),
    ]
    assert w.notes[1].mention == "<!here>" and "ESCALATED" in w.notes[1].text
    assert "Nothing was executed" in w.notes[-1].text
    assert approval_payload(st)["approved"] is False
    with pytest.raises(NotPending):
        w.approve("alice")  # a late approval does not bring it back
    assert w.state().status == "expired"


def test_a_long_gap_catches_up_through_every_level(w: World) -> None:
    w.open()
    w.now.t += 10_000
    w.gate.tick()
    assert [e["kind"] for e in w.store.journal("r1.approval").events() if e["kind"] in ("escalated", "resolved")] == [
        "escalated",
        "escalated",
        "resolved",
    ]
    assert w.state().status == "expired"
    assert w.gate.tick() == []  # a resolved request is left alone


def test_the_runs_own_deadline_caps_the_escalation(w: World) -> None:
    w.open(hard_deadline=w.now.t + 150)
    w.now.t += 101
    w.gate.tick()
    assert w.state().level == 1 and w.state().is_last_level  # level 2 would start after the deadline: never entered
    w.now.t += 50
    w.gate.tick()
    assert w.state().status == "expired"


def test_a_decision_applies_timeouts_first(w: World) -> None:
    w.open()
    w.now.t += 1000
    with pytest.raises(NotPending, match="expired"):
        w.approve("alice")
    assert w.state().status == "expired" and w.state().approvals == ()


# ------------------------------------------------------------------------------------------------ deciding


def test_approval_at_the_first_level(w: World) -> None:
    w.open()
    st = w.approve("alice", comment="checked the logs token=abcdefghijklmnop")
    assert (
        st.status == "approved"
        and st.decided_by == ("alice",)
        and st.approvals[0]["comment"].count("abcdefghijklmnop") == 0
    )
    assert approval_payload(st) == {"approved": True, "by": "alice", "request": "r1.approval"}
    assert (
        w.notes[-1].kind == "resolved"
        and "approved" in w.notes[-1].text
        and "Nothing was executed" not in w.notes[-1].text
    )
    with pytest.raises(NotPending):
        w.reject("bob")


def test_who_may_answer_grows_with_escalation(w: World) -> None:
    w.open()
    for who in ("bob", "carol", "zed"):
        with pytest.raises(NotEligible):
            w.approve(who)  # their level has not been asked yet (or never is)
    w.now.t += 101
    w.gate.tick()
    assert w.state().level == 1
    with pytest.raises(NotEligible):
        w.approve("carol")
    w.approve("bob")  # the secondary level answers now
    w2 = World(w.store.root.parent / "second")
    w2.open()
    w2.now.t += 201
    w2.gate.tick()
    assert w2.approve("alice").status == "approved"  # the first level may still answer after an escalation


def test_quorum_needs_distinct_approvers(tmp_path: Path) -> None:
    w = World(tmp_path, required=2)
    w.open()
    assert w.state().required == 2
    st = w.approve("alice")
    assert st.status == "pending" and len(st.approvals) == 1
    with pytest.raises(DuplicateApproval):
        w.approve("alice")
    st = w.approve("dave")
    assert st.status == "approved" and st.decided_by == ("alice", "dave")
    assert approval_payload(st)["by"] == "alice+dave"


def test_any_eligible_rejection_is_final(tmp_path: Path) -> None:
    w = World(tmp_path, required=2)
    w.open()
    w.approve("alice")
    st = w.reject("dave", comment="not during the sale")
    assert st.status == "rejected" and st.decided_by == ("dave",) and approval_payload(st)["approved"] is False
    with pytest.raises(NotPending):
        w.approve("alice")
    w3 = World(tmp_path / "x")
    w3.open()
    with pytest.raises(NotEligible):
        w3.reject("zed")  # a stranger cannot veto either
    assert w3.state().status == "pending"


def test_authentication_failures_are_logged_and_capped(w: World) -> None:
    w.open()
    with pytest.raises(AuthError):
        w.gate.approve("r1.approval", "alice", "wrong", confirm=CONFIRM)
    with pytest.raises(AuthError):
        w.gate.approve("r1.approval", "mallory", "wrong", confirm=CONFIRM)
    assert w.state().denied == 2 and w.state().status == "pending"
    for _ in range(80):
        with pytest.raises(AuthError):
            w.gate.reject("r1.approval", "alice", "wrong")
    assert w.state().denied == 50  # logged up to the cap, refused every time


def test_the_confirmation_must_name_the_action(w: World) -> None:
    w.open()
    for bad in ("", "deadbeef", CONFIRM[:4], None):
        with pytest.raises(BadConfirmation):
            w.approve("alice", confirm=bad)  # type: ignore[arg-type]
    assert w.state().status == "pending" and w.state().denied == 4
    assert w.approve("alice", confirm=f"  {CONFIRM.upper()} ").status == "approved"


def test_the_requester_cannot_decide_their_own_request(w: World) -> None:
    w.open(requested_by="alice")
    with pytest.raises(SelfApproval):
        w.approve("alice")
    with pytest.raises(SelfApproval):
        w.reject("alice")
    assert w.approve("dave").status == "approved"


def test_a_removed_approver_loses_access_at_once(w: World) -> None:
    w.open()
    w.roster.remove("alice")
    with pytest.raises(AuthError):
        w.approve("alice")
    assert w.approve("dave").status == "approved"


def test_two_people_deciding_at_once_resolve_it_exactly_once(w: World) -> None:
    w.open()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def go(who: str) -> None:
        barrier.wait()
        try:
            w.approve(who)
            results.append("ok")
        except NotPending:
            results.append("late")

    threads = [threading.Thread(target=go, args=(n,)) for n in ("alice", "dave")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["late", "ok"] and w.kinds().count("resolved") == 1 and len(w.state().approvals) == 1


# ------------------------------------------------------------------------------------------------ notifications


def test_failed_notifications_are_retried_then_give_up(tmp_path: Path) -> None:
    w = World(tmp_path, fail=True)
    w.open()
    assert w.state().notify_attempts("opened", 0) == 1 and not w.state().notified("opened", 0)
    for _ in range(10):
        w.gate.tick()
    assert w.state().notify_attempts("opened", 0) == 5  # bounded
    assert len(w.notes) == 5 and w.state().pending
    w.fail = False
    w.now.t += 101
    w.gate.tick()  # the next level gets its own attempts
    assert w.state().notified("escalated", 1)


def test_a_notifier_that_raises_never_loses_the_request(tmp_path: Path) -> None:
    def boom(_n: Notification) -> bool:
        raise RuntimeError("pager is down")

    now = Now()
    roster = Roster(tmp_path / "r.json")
    tok = roster.add("alice", ["oncall"])
    gate = ApprovalGate(RequestStore(tmp_path / "s", now), roster, policy(), CallbackNotifier(boom), now)
    gate.open("r.approval", run_id="r", incident_id="I", command_line="x", fingerprint=FP, tier="high", reason="r")
    st = gate.store.state("r.approval")
    assert st.pending and st.notifications[0]["ok"] is False and st.notifications[0]["detail"] == "RuntimeError"
    assert gate.approve("r.approval", "alice", tok, confirm=CONFIRM).status == "approved"


def test_the_resolution_notice_is_retried_after_the_decision(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.open()
    w.fail = True
    w.approve("alice")
    assert w.state().status == "approved" and not w.state().notified("resolved", 0)
    w.fail = False
    out = w.gate.tick()
    assert out and out[0]["status"] == "approved" and w.state().notified("resolved", 0)
    assert w.gate.tick() == []  # delivered now: nothing left to do


def test_a_restarted_gate_does_not_repeat_notifications(w: World, tmp_path: Path) -> None:
    w.open()
    w.now.t += 101
    w.gate.tick()
    before = len(w.notes)
    fresh = ApprovalGate(
        RequestStore(tmp_path / "requests", w.now), w.roster, policy(), CallbackNotifier(lambda n: True), w.now
    )
    assert fresh.tick() == [] and len(w.notes) == before and fresh.store.state("r1.approval").level == 1


def test_tick_skips_a_request_another_process_holds(w: World) -> None:
    w.open()
    w.now.t += 101
    holder = RunLock(w.store.dir("r1.approval"))
    holder.acquire()
    try:
        assert w.gate.tick() == []
    finally:
        holder.release()
    assert len(w.gate.tick()) == 1


def test_cancel_and_delivery_marks(w: World) -> None:
    w.open()
    st = w.gate.cancel("r1.approval", "run finished")
    assert st.status == "cancelled" and w.gate.cancel("r1.approval", "again").status == "cancelled"
    assert not st.delivered
    w.gate.mark_delivered("r1.approval")
    w.gate.mark_delivered("r1.approval")
    assert w.state().delivered and w.kinds().count("delivered") == 1
    w2 = World(w.store.root.parent / "other")
    w2.open()
    w2.now.t += 5000
    assert w2.gate.cancel("r1.approval", "x").status == "expired"  # time wins over a cancel


def test_notification_text_is_sanitised() -> None:
    from incident_agent.hitl.store import fold as _fold  # noqa: F401

    n = Now()
    events = [
        {
            "kind": "request_opened",
            "run_id": "r",
            "incident_id": "INC-1 <!channel>",
            "command_line": "restart-service `x`\nsecret=sk-abcdefghijklmnopqrstuvwx",
            "fingerprint": FP,
            "tier": "high",
            "reason": "r",
            "opened_at": n.t,
            "required": 1,
            "levels": policy().to_json()["levels"],
            "deadlines": [n.t + 1, n.t + 2, n.t + 3],
        }
    ]
    note = build_notification(fold("r", events), "opened")
    assert "\n" not in note.text and "sk-abcdefghijklmnopqrstuvwx" not in note.text
    sent: list[dict[str, Any]] = []

    class Stub:
        name = "stub"

        def send(self, payload: Any) -> SendResult:
            sent.append(dict(payload))
            return SendResult(True, "delivered")

    assert SlackNotifier(Stub()).send(note).ok  # type: ignore[arg-type]
    assert "<!channel>" not in sent[0]["text"] and "&lt;!channel&gt;" in sent[0]["text"]
    with_mention = Notification("escalated", "r", 1, "secondary", ("lead",), "hi", "<!here>")
    SlackNotifier(Stub()).send(with_mention)  # type: ignore[arg-type]
    assert sent[1]["text"].startswith("<!here> ")
    with pytest.raises(ValueError):
        SlackNotifier(Stub()).send(Notification("escalated", "r", 1, "s", ("l",), "hi", "<!everyone>"))  # type: ignore[arg-type]


def test_notifier_implementations(tmp_path: Path) -> None:
    note = Notification("opened", "r", 0, "primary", ("oncall",), "hello")
    box = OutboxNotifier(tmp_path / "o" / "out.jsonl")
    assert box.send(note).ok and json.loads((tmp_path / "o" / "out.jsonl").read_text())["text"] == "hello"
    blocked = tmp_path / "file"
    blocked.write_text("x")
    assert OutboxNotifier(blocked / "out.jsonl").send(note).ok is False
    assert CallbackNotifier(lambda n: None).send(note).ok and not CallbackNotifier(lambda n: False).send(note).ok

    class Bad:
        name = "bad"

        def send(self, n: Notification) -> NotifyResult:
            raise OSError("x")

    class Ok:
        name = "ok"

        def send(self, n: Notification) -> NotifyResult:
            return NotifyResult(True)

    multi = MultiNotifier([Ok(), Bad(), CallbackNotifier(lambda n: False)])
    r = multi.send(note)
    assert not r.ok and "bad: OSError" in r.detail and "callback" in r.detail
    assert MultiNotifier([Ok()]).send(note).ok
    with pytest.raises(ValueError):
        MultiNotifier([])


# --------------------------------------------------------------------------------------- bridge to durable runs


class Clk:
    """Engine clock and gate clock in one, so durable deadlines and escalation agree."""

    def __init__(self) -> None:
        self.t = 5_000_000.0

    def __call__(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Rig:
    def __init__(self, tmp: Path, *, required: int = 1, data: dict[str, Any] | None = None) -> None:
        self.clk = Clk()
        self.backend = FakeBackend()
        d = data or DATA
        pol = policy(required)
        deps = wf.Deps(
            backend_factory=lambda: self.backend, data_loader=lambda _i: d, approval_timeout=pol.total_timeout
        )
        from incident_agent.durable.engine import Clock

        class _C(Clock):
            def now(s) -> float:  # noqa: N805
                return self.clk.t

            def sleep(s, seconds: float) -> None:  # noqa: N805
                self.clk.t += seconds

        self.engine = Engine(wf.incident_workflow(deps), tmp / "runs", clock=_C(), poll_interval=0.5)
        self.roster = Roster(tmp / "roster.json")
        self.tokens = {
            n: self.roster.add(n, [r]) for n, r in (("alice", "oncall"), ("bob", "lead"), ("carol", "manager"))
        }
        self.notes: list[Notification] = []
        self.gate = ApprovalGate(
            RequestStore(tmp / "requests", self.clk),
            self.roster,
            pol,
            CallbackNotifier(lambda n: self.notes.append(n)),
            self.clk,
        )
        self.ctl = Controller(self.gate, self.engine)

    def start(self, run_id: str = "r1", **extra: Any) -> None:
        self.engine.start(run_id, {"incident_id": "INC-777", **extra})
        self.engine.run(run_id, block=False)

    def rid(self, run_id: str = "r1") -> str:
        return request_id_for(run_id)

    def fp(self, run_id: str = "r1") -> str:
        return self.gate.store.state(self.rid(run_id)).fingerprint[:8]


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_waiting_run_gets_a_request_with_the_runs_own_deadline(rig: Rig) -> None:
    rig.start()
    report = rig.ctl.step()
    assert report["opened"] == ["r1.approval"] and report["errors"] == []
    st = rig.gate.store.state("r1.approval")
    wait_deadline = next(e["deadline"] for e in rig.engine._journal("r1").events() if e["kind"] == "wait_started")  # noqa: SLF001
    assert st.hard_deadline == wait_deadline and st.command_line == "restart-service checkout-api --reason oom"
    assert st.incident_id == "INC-777" and st.run_id == "r1" and "OOMKilled" in st.reason
    assert rig.ctl.step()["opened"] == []  # opening again changes nothing


def test_approval_flows_back_and_the_run_applies_it_once(rig: Rig) -> None:
    rig.start()
    rig.ctl.step()
    rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())
    report = rig.ctl.step()
    assert report["delivered"] == ["r1.approval"]
    final = rig.engine.state("r1")
    ex = final.results["execute"]
    assert final.status == "completed" and ex["mode"] == "applied" and ex["approved_by"] == "alice"
    assert rig.backend.calls == 1
    assert rig.ctl.step()["delivered"] == []  # a decision is carried over once, not on every pass
    rig.ctl.step()
    assert rig.backend.calls == 1 and rig.gate.store.state(rig.rid()).delivered
    from incident_agent.durable.journal import read_signals

    assert len(read_signals(rig.engine.run_dir("r1"))) == 1


def test_rejection_and_silence_both_end_in_a_dry_run(tmp_path: Path) -> None:
    for how in ("reject", "silence"):
        r = Rig(tmp_path / how)
        r.start()
        r.ctl.step()
        if how == "reject":
            r.gate.reject(r.rid(), "alice", r.tokens["alice"])
            r.ctl.step()  # the controller carries the decision to the run
        else:
            for _ in range(4):
                r.clk.t += 101
                r.ctl.step()
        st = r.engine.state("r1")
        assert st.status == "completed" and st.results["execute"]["mode"] == "dry_run", how
        assert (
            st.results["execute"]["changed"] is False and r.backend.calls == 1
        )  # the dry run still goes through the sandbox
        assert r.gate.store.state(r.rid()).status in ("rejected", "expired")


def test_two_approvers_across_an_escalation_apply_it(tmp_path: Path) -> None:
    r = Rig(tmp_path, required=2)
    r.start()
    r.ctl.step()
    r.gate.approve(r.rid(), "alice", r.tokens["alice"], confirm=r.fp())
    r.clk.t += 101
    r.ctl.step()
    assert r.gate.store.state(r.rid()).level == 1 and r.engine.state("r1").status == "waiting"
    r.gate.approve(r.rid(), "bob", r.tokens["bob"], confirm=r.fp())
    r.ctl.step()
    assert r.engine.state("r1").results["execute"]["approved_by"] == "alice+bob"


def test_the_requester_recorded_in_the_run_cannot_approve(rig: Rig) -> None:
    rig.start(requested_by="alice")
    rig.ctl.step()
    with pytest.raises(SelfApproval):
        rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())


def test_a_run_that_stops_waiting_cancels_its_request(rig: Rig) -> None:
    rig.start()
    rig.ctl.step()
    rig.engine.cancel("r1")
    rig.engine.run("r1", block=False)
    report = rig.ctl.step()
    assert report["cancelled"] == ["r1.approval"] and rig.gate.store.state(rig.rid()).status == "cancelled"
    assert rig.engine.state("r1").status == "cancelled"
    with pytest.raises(NotPending):
        rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())


def test_the_run_timing_out_first_cancels_the_request(rig: Rig) -> None:
    rig.start()
    rig.ctl.step()
    rig.clk.t += 100_000  # far beyond the wait deadline; the engine times out when it runs
    rig.engine.run("r1", block=False)
    assert rig.engine.state("r1").results["approval"]["timed_out"] is True
    rig.ctl.step()
    assert rig.gate.store.state(rig.rid()).status in ("cancelled", "expired")


def test_read_only_plans_never_open_a_request(tmp_path: Path) -> None:
    r = Rig(tmp_path, data={**DATA, "logs": [DATA["logs"][0]]})
    r.start()
    assert r.engine.state("r1").status == "completed"
    assert r.ctl.step()["opened"] == [] and r.gate.store.ids() == []


def test_a_decision_for_a_run_that_is_gone_is_marked_delivered(rig: Rig) -> None:
    rig.start()
    rig.ctl.step()
    rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())
    rig.engine.cancel("r1")
    rig.engine.run("r1", block=False)
    report = rig.ctl.step()
    assert report["errors"] == [] and rig.gate.store.state(rig.rid()).delivered
    assert rig.backend.calls == 0  # nothing was executed for a cancelled run
    from incident_agent.durable.journal import read_signals

    assert [x["name"] for x in read_signals(rig.engine.run_dir("r1"))] == ["cancel"]  # no stray approval was sent


def test_a_duplicate_delivery_cannot_run_the_action_twice(rig: Rig) -> None:
    rig.start()
    rig.ctl.step()
    rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())
    st = rig.gate.store.state(rig.rid())
    # a crash between "signal sent" and "delivered recorded" leads to the same signal being sent twice
    rig.engine.signal("r1", "approval", approval_payload(st))
    rig.engine.signal("r1", "approval", approval_payload(st))
    rig.ctl.step()
    rig.ctl.step()
    assert rig.backend.calls == 1 and rig.engine.state("r1").status == "completed"


def test_drive_false_leaves_resuming_to_someone_else(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.ctl = Controller(r.gate, r.engine, drive=False)
    r.start()
    r.ctl.step()
    r.gate.approve(r.rid(), "alice", r.tokens["alice"], confirm=r.fp())
    r.ctl.step()
    assert r.engine.state("r1").status == "waiting" and r.backend.calls == 0
    r.engine.run("r1", block=False)
    assert r.engine.state("r1").results["execute"]["mode"] == "applied"


def test_controller_reports_broken_runs_and_keeps_going(rig: Rig) -> None:
    rig.start()
    bad = rig.engine.run_dir("zz-broken")
    bad.mkdir(parents=True)
    (bad / "journal.jsonl").write_text("garbage\nmore garbage\nthird\n")
    report = rig.ctl.step()
    assert any("zz-broken" in e for e in report["errors"]) and report["opened"] == ["r1.approval"]


def test_a_worker_holding_the_run_does_not_lose_the_decision(rig: Rig) -> None:
    rig.start()
    rig.ctl.step()
    rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())
    from incident_agent.durable.journal import RunLock as Lock

    busy = Lock(rig.engine.run_dir("r1"))
    busy.acquire()
    try:
        rig.ctl.step()  # signal is delivered, but resuming has to wait for the other worker
        assert rig.engine.state("r1").status == "waiting"
    finally:
        busy.release()
    rig.ctl.step()
    assert rig.engine.state("r1").results["execute"]["mode"] == "applied" and rig.backend.calls == 1


def test_approval_payload_shapes() -> None:
    n = Now()
    base = {
        "kind": "request_opened",
        "run_id": "r",
        "incident_id": "I",
        "command_line": "x",
        "fingerprint": FP,
        "tier": "high",
        "reason": "r",
        "opened_at": n.t,
        "required": 1,
        "levels": policy().to_json()["levels"],
        "deadlines": [1.0, 2.0, 3.0],
    }
    for outcome, approved in (("approved", True), ("rejected", False), ("expired", False), ("cancelled", False)):
        st = fold("r", [base, {"kind": "resolved", "outcome": outcome, "reason": "", "by": ["a", "b"], "at": 1.0}])
        p = approval_payload(st)
        assert p["approved"] is approved and p["request"] == "r" and bool(p["by"])
        assert ("reason" in p) is (not approved)


# ------------------------------------------------------------------------------------------------ command line


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    backend = FakeBackend()
    monkeypatch.setattr(cli, "select_backend", lambda _n=None: backend)
    monkeypatch.delenv(cli.TOKEN_ENV, raising=False)
    monkeypatch.setenv("INCIDENT_RUNS_DIR", str(tmp_path / "runs"))
    return ["--root", str(tmp_path / "hitl")]


def test_cli_roster_and_end_to_end(
    cli_env: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main([*cli_env, "roster", "list"]) == 0 and capsys.readouterr().out == ""
    assert cli.main([*cli_env, "roster", "add", "alice", "--role", "oncall"]) == 0
    out = capsys.readouterr().out
    token = out.strip().splitlines()[-1]
    assert "shown once" in out and len(token) > 20
    assert cli.main([*cli_env, "roster", "list"]) == 0 and "alice  roles: oncall" in capsys.readouterr().out
    assert cli.main([*cli_env, "roster", "add", "Bad Name", "--role", "x"]) == 2
    capsys.readouterr()
    assert cli.main([*cli_env, "list"]) == 0 and "no requests" in capsys.readouterr().out
    assert cli.main([*cli_env, "start", "INC-002", "--run-id", "run1", "--requested-by", "bob"]) == 0
    assert "waiting for approval" in capsys.readouterr().out
    assert cli.main([*cli_env, "serve", "--once"]) == 0 and "opened: run1.approval" in capsys.readouterr().out
    assert (
        cli.main([*cli_env, "list", "--status", "pending"]) == 0 and "run1.approval  pending" in capsys.readouterr().out
    )
    assert cli.main([*cli_env, "list", "--status", "approved"]) == 0 and "no requests" in capsys.readouterr().out
    assert cli.main([*cli_env, "show", "run1.approval"]) == 0
    shown = capsys.readouterr().out
    assert "PENDING" in shown and "silence escalates" in shown
    fp = next(line for line in shown.splitlines() if "fingerprint" in line).split("fingerprint ")[1].rstrip(")")[:8]
    assert (
        cli.main([*cli_env, "approve", "run1.approval", "--by", "alice", "--confirm", fp]) == 2
    )  # no token in the env
    assert cli.TOKEN_ENV in capsys.readouterr().err
    monkeypatch.setenv(cli.TOKEN_ENV, "wrong")
    assert cli.main([*cli_env, "approve", "run1.approval", "--by", "alice", "--confirm", fp]) == 1
    assert "refused: unknown approver or wrong token" in capsys.readouterr().err
    monkeypatch.setenv(cli.TOKEN_ENV, token)
    assert cli.main([*cli_env, "approve", "run1.approval", "--by", "alice", "--confirm", "00000000"]) == 1
    assert "first 8 characters" in capsys.readouterr().err
    assert cli.main([*cli_env, "approve", "run1.approval", "--by", "alice", "--confirm", fp, "--comment", "ok"]) == 0
    assert "APPROVED" in capsys.readouterr().out
    assert cli.main([*cli_env, "approve", "run1.approval", "--by", "alice", "--confirm", fp]) == 1  # already resolved
    capsys.readouterr()
    assert cli.main([*cli_env, "serve", "--once"]) == 0 and "delivered: run1.approval" in capsys.readouterr().out
    assert cli.main([*cli_env, "roster", "remove", "alice"]) == 0 and "removed" in capsys.readouterr().out
    assert cli.main([*cli_env, "roster", "remove", "alice"]) == 0 and "no such approver" in capsys.readouterr().out


def test_cli_reject_and_errors(
    cli_env: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    token = cli.Roster(Path(cli_env[1]) / "roster.json").add("alice", ["oncall"])
    cli.main([*cli_env, "start", "INC-002", "--run-id", "run2"])
    cli.main([*cli_env, "serve", "--once"])
    capsys.readouterr()
    monkeypatch.setenv(cli.TOKEN_ENV, token)
    assert cli.main([*cli_env, "reject", "run2.approval", "--by", "alice", "--comment", "no"]) == 0
    assert "REJECTED" in capsys.readouterr().out
    assert cli.main([*cli_env, "show", "nope"]) == 2 and "unknown request" in capsys.readouterr().err
    assert cli.main([*cli_env, "--policy", "missing.json", "list"]) == 2
    capsys.readouterr()
    assert cli.main([*cli_env, "serve", "--once"]) == 0


def test_cli_uses_policy_file_in_the_root_and_reports_serve_errors(
    cli_env: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(cli_env[1])
    root.mkdir(parents=True)
    (root / "policy.json").write_text(json.dumps({"levels": [{"name": "solo", "roles": ["oncall"], "timeout": 60}]}))
    cli.main([*cli_env, "start", "INC-002", "--run-id", "run3"])
    bad = Path(os.environ["INCIDENT_RUNS_DIR"]) / "zz"
    bad.mkdir(parents=True)
    (bad / "journal.jsonl").write_text("a\nb\nc\n")
    assert cli.main([*cli_env, "serve", "--once"]) == 1  # a broken run is reported, the rest still processed
    out = capsys.readouterr().out
    assert "opened: run3.approval" in out and "errors:" in out
    assert cli.main([*cli_env, "show", "run3.approval"]) == 0 and "1/1 'solo'" in capsys.readouterr().out


def test_cli_describe_shows_failures_and_denials(w: World) -> None:
    w.fail = True
    w.open()
    with pytest.raises(AuthError):
        w.gate.reject("r1.approval", "alice", "wrong")
    text = cli.describe(w.state())
    assert "notification attempt(s) failed" in text and "refused attempt(s)" in text
    w.fail = False
    w.approve("alice")
    done = cli.describe(w.state())
    assert "APPROVED" in done and "resolved" in done and "silence" not in done
    assert cli._when(None) == "-"  # noqa: SLF001


def test_cli_demo_end_to_end(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli.main(["--root", str(tmp_path / "demo"), "demo", "--backend", "process"]) == 0
    out = capsys.readouterr().out
    assert "request: expired; run: completed; execution: mode dry_run, status ok" in out
    assert "approved by: alice+bob" in out
    assert "RESULT: silence escalated and expired without changes" in out
    assert "sandbox result: refused safely by the backend" in out


def test_cli_demo_default_root(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["demo"]) == 0
    assert "state directory:" in capsys.readouterr().out


def test_roster_entry_with_malformed_roles_cannot_authenticate(tmp_path: Path) -> None:
    from incident_agent.hitl.roster import _digest

    (tmp_path / "roster.json").write_text(json.dumps({"a": {"roles": "oncall", "token_sha256": _digest("tok")}}))
    with pytest.raises(AuthError):
        Roster(tmp_path / "roster.json").authenticate("a", "tok")


def test_a_failure_while_resuming_after_delivery_is_reported(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_agent.durable.journal import JournalError

    rig.start()
    rig.ctl.step()
    rig.gate.approve(rig.rid(), "alice", rig.tokens["alice"], confirm=rig.fp())
    run = rig.engine.run

    def broken(*_a: Any, **_k: Any) -> Any:
        raise JournalError("disk")

    monkeypatch.setattr(rig.engine, "run", broken)
    report = rig.ctl.step()
    assert report["delivered"] == ["r1.approval"] and any("JournalError" in e for e in report["errors"])
    monkeypatch.setattr(rig.engine, "run", run)
    rig.ctl.step()
    assert rig.engine.state("r1").status == "completed" and rig.backend.calls == 1


def test_cli_default_root_and_the_serve_loop(
    cli_env: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INCIDENT_HITL_DIR", str(tmp_path / "viaenv"))
    assert cli.default_hitl_root() == tmp_path / "viaenv"
    monkeypatch.delenv("INCIDENT_HITL_DIR")
    assert cli.default_hitl_root() == Path(".cache/hitl")

    def stop(_s: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    assert cli.main([*cli_env, "serve", "--interval", "0.01"]) == 0  # one pass, then Ctrl+C


def test_the_gate_matches_the_high_risk_rules_in_the_spec(tmp_path: Path) -> None:
    """openspec/risk-classification.md, high risk: block until approved, escalate on timeout, never auto-approve."""
    spec = (Path(__file__).resolve().parent.parent / "openspec" / "risk-classification.md").read_text(encoding="utf-8")
    for rule in (
        "Blocks execution until an explicit human approval is received",
        "Escalates to a human/on-call channel on timeout",
        'Never allows a default "auto-approve on timeout"',
    ):
        assert rule in spec
    w = World(tmp_path)
    w.open()
    assert w.state().status == "pending"  # 1. blocked: nothing approves it by itself
    assert approval_payload(w.state())["approved"] is False
    w.now.t += 101
    w.gate.tick()
    assert w.state().level == 1 and w.notes[-1].kind == "escalated"  # 2. timeout escalates to the next channel
    w.now.t += 10_000
    w.gate.tick()
    assert w.state().status == "expired" and approval_payload(w.state())["approved"] is False  # 3. never auto-approve
    assert "approved" not in w.kinds()


def test_random_histories_never_approve_without_enough_valid_approvals(tmp_path: Path) -> None:
    import random

    for seed in range(40):
        rnd = random.Random(seed)
        w = World(tmp_path / f"s{seed}", required=rnd.choice([1, 2, 3]), timeout=rnd.choice([10.0, 50.0]))
        w.open(requested_by=rnd.choice([None, "alice"]))
        last_level, resolved_events = 0, None
        for _ in range(60):
            op = rnd.choice(["time", "tick", "approve", "approve", "reject", "bad_token", "bad_confirm", "cancel"])
            who = rnd.choice(["alice", "dave", "bob", "carol", "zed"])
            try:
                if op == "time":
                    w.now.t += rnd.uniform(0, 80)
                elif op == "tick":
                    w.gate.tick()
                elif op == "approve":
                    w.approve(who)
                elif op == "reject":
                    w.reject(who)
                elif op == "bad_token":
                    w.gate.approve("r1.approval", who, "nope", confirm=CONFIRM)
                elif op == "bad_confirm":
                    w.approve(who, confirm="ffffffff")
                else:
                    if rnd.random() < 0.1:
                        w.gate.cancel("r1.approval", "random")
            except Exception as exc:  # noqa: BLE001 - refusals are part of the model; anything else is a bug
                assert type(exc).__name__ in {
                    "NotPending",
                    "NotEligible",
                    "SelfApproval",
                    "DuplicateApproval",
                    "BadConfirmation",
                    "AuthError",
                }, exc
            st = w.state()
            by = [a["by"] for a in st.approvals]
            assert len(by) == len(set(by)) and st.level >= last_level and "zed" not in by
            assert st.requested_by is None or st.requested_by not in by
            last_level = st.level
            if st.outcome == "approved":
                assert len(by) >= st.required and set(st.decided_by) <= set(by)
                assert "rejected" not in w.kinds()
            if st.outcome in ("expired", "cancelled", "rejected"):
                assert approval_payload(st)["approved"] is False
            if st.outcome is not None:
                snapshot = [
                    e["kind"]
                    for e in w.store.journal("r1.approval").events()
                    if e["kind"] in ("approved", "resolved", "rejected", "escalated")
                ]
                if resolved_events is None:
                    resolved_events = snapshot
                assert snapshot == resolved_events  # a resolved request never changes its decision history


def test_cli_live_slack_needs_a_webhook_and_posts_through_the_stage_15_sender(
    cli_env: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert cli.main(["--live-slack", *cli_env, "list"]) == 2 and "SLACK_WEBHOOK_URL" in capsys.readouterr().err
    posted: list[dict[str, Any]] = []

    class FakeSender:
        name = "fake"

        def __init__(self, url: str) -> None:
            assert url == "https://hooks.slack.com/services/T/B/x"

        def send(self, payload: Any) -> SendResult:
            posted.append(dict(payload))
            return SendResult(True, "delivered")

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    monkeypatch.setattr(cli, "WebhookSender", FakeSender)
    cli.main([*cli_env, "start", "INC-002", "--run-id", "live1"])
    assert cli.main(["--live-slack", *cli_env, "serve", "--once"]) == 0
    assert posted and "Needs a human" in posted[0]["text"] and (Path(cli_env[1]) / "outbox.jsonl").exists()
    capsys.readouterr()
    assert cli.main([*cli_env, "serve", "--once"]) == 0  # without the flag nothing more is posted
    assert len(posted) == 1
