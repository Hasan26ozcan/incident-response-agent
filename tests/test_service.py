"""Stage 21f: the HTTP service, over a real socket (scripted LLM, real engine, real sandbox backend, real lab data)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from incident_agent.agenteval.rig import RecordingBackend
from incident_agent.security.killswitch import KillSwitch
from incident_agent.service.__main__ import main as cli_main
from incident_agent.service.core import Service, ServiceConfig
from incident_agent.service.http import make_server
from incident_agent.service.tokens import TokenStore
from tests.test_pipeline import script

MEM = "checkout-api retains memory until the heap limit is exceeded: OutOfMemory"


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

        class R:
            ok = True

        return R()


class Rig:
    def __init__(self, tmp_path, **cfg):
        self.tokens = TokenStore(tmp_path / "tokens.json")
        self.read = self.tokens.add("viewer", ["read"])
        self.sub = self.tokens.add("ci", ["read", "submit"])
        self.sender = Sender()
        self.backend = RecordingBackend()
        cfg.setdefault("client_factory", lambda: script(MEM))
        cfg.setdefault("backend_factory", lambda: self.backend)
        self.svc = Service(ServiceConfig(root=tmp_path / "svc", sender=self.sender, control_interval=0.05, **cfg))
        self.srv = make_server(self.svc, self.tokens, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.svc.start()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method, path, token=None, body=None, headers=None, raw=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                txt = r.read().decode()
                return r.status, (json.loads(txt) if "json" in r.headers.get("Content-Type", "") else txt)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def wait(self, run_id, status, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            _c, body = self.call("GET", f"/api/runs/{run_id}", self.read)
            if body.get("status") in status:
                return body
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} never reached {status}: {body}")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.svc.stop()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


class TestAuth:
    def test_public_endpoints_need_no_token_and_leak_nothing(self, rig):
        assert rig.call("GET", "/healthz") == (200, {"status": "ok"})
        code, body = rig.call("GET", "/readyz")
        assert code == 200 and body["worker_alive"] and body["kill_switch"] == "off"
        assert rig.call("GET", "/")[0] == 200

    @pytest.mark.parametrize("path", ["/api/runs", "/api/requests", "/metrics", "/api/runs/lab-002-123456"])
    def test_api_needs_a_token(self, rig, path):
        assert rig.call("GET", path)[0] == 401
        assert rig.call("GET", path, "ira_wrong")[0] == 401

    def test_read_token_cannot_submit(self, rig):
        code, _ = rig.call("POST", "/api/incidents", rig.read, {"incident_id": "LAB-002"})
        assert code == 403 and not rig.svc.jobs

    def test_failed_logins_are_throttled(self, rig):
        codes = [rig.call("GET", "/api/runs", "ira_bad")[0] for _ in range(12)]
        assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
        assert rig.call("GET", "/api/runs", rig.read)[0] == 429  # even a good token waits out the window

    def test_wrong_method_and_unknown_path(self, rig):
        assert rig.call("DELETE", "/api/runs", rig.read)[0] == 405
        assert rig.call("GET", "/nope", rig.read)[0] == 404


class TestSubmit:
    def test_shadow_investigation_end_to_end(self, rig):
        code, body = rig.call("POST", "/api/incidents", rig.sub, {"incident_id": "LAB-002"})
        assert code == 202
        run = rig.wait(body["run_id"], ("completed", "failed"))
        assert run["status"] == "completed" and run["diagnosis"]["category"] and run["by"] == "ci"
        assert rig.backend.applied == 0  # shadow mode: nothing was changed
        assert rig.sender.sent and "LAB-002" in rig.sender.sent[0]["text"] and "shadow" in rig.sender.sent[0]["text"]
        assert any(r["run_id"] == body["run_id"] for r in rig.call("GET", "/api/runs", rig.read)[1]["runs"])
        assert "incident_agent_jobs" in rig.call("GET", "/metrics", rig.read)[1]

    @pytest.mark.parametrize("bad", [None, "", "../INC-001", "inc-001", "LAB-002; rm -rf /", 7, ["LAB-002"]])
    def test_bad_ids_refused(self, rig, bad):
        assert rig.call("POST", "/api/incidents", rig.sub, {"incident_id": bad})[0] == 400

    def test_unknown_incident_is_404(self, rig):
        assert rig.call("POST", "/api/incidents", rig.sub, {"incident_id": "INC-999"})[0] == 404

    def test_body_rules(self, rig):
        assert rig.call("POST", "/api/incidents", rig.sub, raw=b"{not json")[0] == 400
        assert rig.call("POST", "/api/incidents", rig.sub, raw=b"[1]")[0] == 400
        assert rig.call("POST", "/api/incidents", rig.sub, raw=b"x" * 9000)[0] == 413
        assert (
            rig.call(
                "POST",
                "/api/incidents",
                rig.sub,
                body={"incident_id": "LAB-002"},
                headers={"Content-Type": "text/plain"},
            )[0]
            == 415
        )
        assert rig.call("POST", "/api/incidents", rig.sub, raw=b"")[0] == 400

    def test_duplicate_active_incident_is_not_queued_twice(self, rig):
        gate = threading.Event()
        rig.svc.cfg.client_factory = lambda: (gate.wait(5), script(MEM))[1]
        a = rig.call("POST", "/api/incidents", rig.sub, {"incident_id": "LAB-002"})
        b = rig.call("POST", "/api/incidents", rig.sub, {"incident_id": "LAB-002"})
        gate.set()
        assert a[0] == 202 and b[0] == 200 and b[1]["duplicate"] and a[1]["run_id"] == b[1]["run_id"]
        rig.wait(a[1]["run_id"], ("completed", "failed"))

    def test_rate_limit_and_full_queue(self, tmp_path):
        r = Rig(tmp_path, rate_per_minute=2)
        try:
            codes = [
                r.call("POST", "/api/incidents", r.sub, {"incident_id": i})[0]
                for i in ("LAB-001", "LAB-003", "LAB-004")
            ]
            assert codes[2] == 429
        finally:
            r.close()

    def test_kill_switch_refuses_new_work(self, rig):
        KillSwitch(rig.svc.root / "killswitch").engage("drill", "ops")
        assert rig.call("POST", "/api/incidents", rig.sub, {"incident_id": "LAB-002"})[0] == 503
        assert rig.call("GET", "/readyz")[1]["kill_switch"] == "engaged"

    def test_failed_investigation_is_reported_not_fatal(self, tmp_path):
        def boom():
            raise RuntimeError("provider exploded with key sk-SECRET")

        r = Rig(tmp_path, client_factory=boom)
        try:
            _c, b = r.call("POST", "/api/incidents", r.sub, {"incident_id": "LAB-002"})
            run = r.wait(b["run_id"], ("failed",))
            assert "RuntimeError" in run["error"] and r.call("GET", "/healthz")[0] == 200
            assert r.svc.counters["failed"] == 1
        finally:
            r.close()

    def test_bad_run_ids(self, rig):
        assert rig.call("GET", "/api/runs/..%2Fetc", rig.read)[0] in (400, 404)
        assert rig.call("GET", "/api/runs/lab-002-999999", rig.read)[0] == 404


class TestAssist:
    def test_approval_flow_over_http(self, tmp_path):
        r = Rig(tmp_path, mode="assist")
        try:
            alice = r.svc.roster.add("alice", ["oncall"])
            _c, b = r.call("POST", "/api/incidents", r.sub, {"incident_id": "LAB-002"})
            run = r.wait(b["run_id"], ("waiting",))
            assert run["plan"]["verdict"] == "needs_approval" and r.backend.applied == 0
            end = time.time() + 20
            reqs = []
            while time.time() < end and not reqs:
                reqs = r.call("GET", "/api/requests", r.read)[1]["requests"]
                time.sleep(0.1)
            assert reqs and reqs[0]["status"] == "pending"
            rid = reqs[0]["id"]
            fp = r.svc.gate.store.state(rid).fingerprint[:8]
            path = f"/api/requests/{rid}/approve"
            # a service token is not an approver token
            assert (
                r.call("POST", path, body={"confirm": fp}, headers={"X-Approver": "alice", "X-Approver-Token": r.sub})[
                    0
                ]
                == 401
            )
            assert (
                r.call(
                    "POST",
                    path,
                    body={"confirm": "deadbeef"},
                    headers={"X-Approver": "alice", "X-Approver-Token": alice},
                )[0]
                == 409
            )
            code, body = r.call(
                "POST", path, body={"confirm": fp}, headers={"X-Approver": "alice", "X-Approver-Token": alice}
            )
            assert code == 200 and body["status"] == "approved"
            done = r.wait(b["run_id"], ("completed",))
            assert done["execution"]["mode"] == "applied" and r.backend.applied == 1
            assert (
                r.call("POST", path, body={"confirm": fp}, headers={"X-Approver": "alice", "X-Approver-Token": alice})[
                    0
                ]
                == 409
            )
        finally:
            r.close()

    def test_shadow_mode_has_no_approvals(self, rig):
        assert rig.call("GET", "/api/requests", rig.read)[1]["requests"] == []
        assert (
            rig.call(
                "POST",
                "/api/requests/x/approve",
                body={"confirm": "a"},
                headers={"X-Approver": "a", "X-Approver-Token": "b"},
            )[0]
            == 409
        )

    def test_kill_switch_blocks_decisions(self, tmp_path):
        r = Rig(tmp_path, mode="assist")
        try:
            KillSwitch(r.svc.root / "killswitch").engage("drill", "ops")
            assert (
                r.call(
                    "POST",
                    "/api/requests/abc/approve",
                    body={"confirm": "a"},
                    headers={"X-Approver": "a", "X-Approver-Token": "b"},
                )[0]
                == 503
            )
        finally:
            r.close()


class TestTokensAndCli:
    def test_digest_only_and_scopes(self, tmp_path):
        ts = TokenStore(tmp_path / "t.json")
        tok = ts.add("bot", ["read"])
        assert tok not in (tmp_path / "t.json").read_text() and ts.authenticate(tok).scopes == {"read"}
        assert ts.authenticate("") is None and ts.authenticate(tok + "x") is None
        with pytest.raises(ValueError):
            ts.add("bot", ["read"])
        for bad in (("admin",), (), ("read", "x")):
            with pytest.raises(ValueError):
                ts.add("other", bad)
        with pytest.raises(ValueError):
            ts.add("Bad Name", ["read"])
        assert ts.remove("bot") and not ts.remove("bot") and ts.authenticate(tok) is None

    def test_corrupt_store_authenticates_nobody(self, tmp_path):
        (tmp_path / "t.json").write_text("{broken")
        assert TokenStore(tmp_path / "t.json").authenticate("ira_x") is None

    def test_cli_token_roster_and_refusals(self, tmp_path, capsys):
        root = str(tmp_path / "s")
        assert cli_main(["--root", root, "token", "add", "ci", "--scopes", "read,submit"]) == 0
        assert "ira_" in capsys.readouterr().out
        assert cli_main(["--root", root, "token", "list"]) == 0 and "ci read,submit" in capsys.readouterr().out
        assert cli_main(["--root", root, "token", "add", "ci"]) == 2
        assert cli_main(["--root", root, "roster", "add", "bob", "--roles", "oncall"]) == 0
        assert cli_main(["--root", root, "roster", "list"]) == 0 and "bob oncall" in capsys.readouterr().out
        assert cli_main(["--root", root, "serve", "--agent", "nope"]) == 2
        assert cli_main(["--root", root, "token", "remove", "ci"]) == 0
        assert cli_main(["--root", root, "serve", "--host", "0.0.0.0"]) == 2  # noqa: S104 - the refusal is the test
        assert cli_main(["--root", root, "serve"]) == 2  # no tokens left


class TestOperations:
    def test_dashboard_is_static_and_hardened(self, rig):
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{rig.port}/") as r:  # noqa: S310
            page, h = r.read().decode(), r.headers
        assert (
            "Content-Security-Policy" in h
            and h["X-Content-Type-Options"] == "nosniff"
            and h["Cache-Control"] == "no-store"
        )
        assert "default-src 'none'" in h["Content-Security-Policy"] and "innerHTML" not in page
        assert rig.read not in page and "Bearer" in page and "localStorage" not in page

    def test_ready_turns_unready_when_the_worker_is_gone(self, tmp_path):
        r = Rig(tmp_path)
        try:
            r.svc.stop()
            code, body = r.call("GET", "/readyz")
            assert code == 503 and body["worker_alive"] is False
        finally:
            r.close()

    def test_waiting_run_blocks_a_duplicate(self, tmp_path):
        r = Rig(tmp_path, mode="assist")
        try:
            _c, a = r.call("POST", "/api/incidents", r.sub, {"incident_id": "LAB-002"})
            r.wait(a["run_id"], ("waiting",))
            code, b = r.call("POST", "/api/incidents", r.sub, {"incident_id": "LAB-002"})
            assert code == 200 and b["duplicate"] and b["run_id"] == a["run_id"]
        finally:
            r.close()

    def test_no_secret_reaches_a_response_or_the_job(self, tmp_path):
        def boom():
            raise RuntimeError("bad key gsk_ABCDEF1234567890ABCDEF1234567890")

        r = Rig(tmp_path, client_factory=boom)
        try:
            _c, b = r.call("POST", "/api/incidents", r.sub, {"incident_id": "LAB-002"})
            run = r.wait(b["run_id"], ("failed",))
            assert "gsk_ABCDEF1234567890ABCDEF1234567890" not in json.dumps(run) and "RuntimeError" in run["error"]
            assert all("gsk_" not in json.dumps(m) for m in r.sender.sent)
        finally:
            r.close()


class TestRestart:
    def test_finished_runs_survive_and_cut_short_runs_are_marked(self, tmp_path):
        r = Rig(tmp_path)
        _c, b = r.call("POST", "/api/incidents", r.sub, {"incident_id": "LAB-002"})
        r.wait(b["run_id"], ("completed",))
        (r.svc.runs_dir / "lab-001-1700000000").mkdir()  # a run whose process died before it wrote its result
        r.close()
        again = Service(ServiceConfig(root=tmp_path / "svc"))
        assert again.get_run(b["run_id"])[1]["status"] == "completed"
        code, body = again.get_run("lab-001-1700000000")
        assert code == 200 and body["status"] == "interrupted" and "stopped" in body["error"]
        assert again.counters["recovered_interrupted"] == 1
        assert {x["run_id"] for x in again.list_runs()["runs"]} >= {b["run_id"], "lab-001-1700000000"}
