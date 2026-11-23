"""Fault lab: the service behaves for real (CPU, memory, timeouts, exceptions), scenarios match the gold schema,
captures are readable by the existing tools, and an end-to-end run with real processes works."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from incident_agent.eval import harness
from incident_agent.lab import app, evaluate, runner
from incident_agent.lab.__main__ import main as cli
from incident_agent.lab.scenarios import SCENARIOS
from incident_agent.tools import find_error_logs, read_deploys, read_logs, read_meta, read_metrics

GOLD_KEYS = {
    "root_cause",
    "evidence",
    "recommended_remediation",
    "remediation_risk_tier",
    "risk_rationale",
    "incident_id",
    "title",
    "category",
    "difficulty",
    "is_false_alarm",
}


@pytest.fixture
def svc(tmp_path):
    s = app.Service("checkout-api", None, tmp_path)
    yield s
    s.stop.set()


class TestService:
    def test_normal_request(self, svc):
        assert svc.handle("/checkout")[0] == 200

    def test_cpu_loop_burns_cpu_and_logs(self, svc):
        svc.set_fault("cpu_loop")
        c0 = time.process_time()
        for _ in range(3):
            svc.handle("/checkout")
        assert time.process_time() - c0 >= 3 * app.BURN_S * 0.8
        assert "unbounded loop" in svc.log_file.read_text()

    def test_memory_leak_grows_then_fails_then_clearing_frees(self, svc, monkeypatch):
        monkeypatch.setattr(app, "HEAP_LIMIT_MB", 20.0)
        monkeypatch.setattr(app, "LEAK_BYTES", 1024 * 1024)
        svc.set_fault("memory_leak")
        codes = [svc.handle("/checkout")[0] for _ in range(30)]
        assert 500 in codes and 200 in codes
        assert "OutOfMemory" in svc.log_file.read_text() and "approaching limit" in svc.log_file.read_text()
        svc.set_fault(None)
        assert svc.leak == []

    def test_bad_config_raises_keyerror_per_request(self, svc):
        svc.deploy("2.4.1", "refactor", broken=True)
        assert svc.handle("/checkout")[0] == 500
        text = svc.log_file.read_text()
        assert (
            "KeyError: 'PAYMENT_API_URL' missing in config for version 2.4.1" in text
            and svc.deploys[0]["version"] == "2.4.1"
        )

    def test_dependency_slow_gives_504(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "SLOW_DELAY_S", 0.6)
        monkeypatch.setattr(app, "DEP_TIMEOUT_S", 0.2)
        pay = app.Service("payments-stub", None, tmp_path)
        srv = app.serve(pay, "127.0.0.1", 0)
        import threading

        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            chk = app.Service("checkout-api", f"http://127.0.0.1:{srv.server_address[1]}", tmp_path)
            assert chk.handle("/checkout")[0] == 200
            pay.set_fault("slow")
            assert chk.handle("/checkout")[0] == 504
            assert "upstream payments-stub timeout after 200ms" in (tmp_path / "checkout-api.log").read_text()
        finally:
            srv.shutdown()
            pay.stop.set()

    def test_dependency_unreachable(self, tmp_path):
        chk = app.Service("checkout-api", f"http://127.0.0.1:{runner.free_port()}", tmp_path)
        assert chk.handle("/checkout")[0] == 504 and "unreachable" in chk.log_file.read_text()

    def test_unknown_fault_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set_fault("nope")

    def test_log_line_format_and_quote_safety(self, svc):
        svc.log("INFO", 'a "quoted"\nmsg')
        line = svc.log_file.read_text().splitlines()[-1]
        assert " service=checkout-api level=INFO msg=" in line and line.count('"') == 2 and "\n" not in line[:-1]

    def test_sampler_metrics(self, svc):
        for _ in range(5):
            svc.handle("/checkout")
        c, w = time.process_time(), time.time() - 1
        svc.sample(c, w)
        assert svc.snap["rps"] > 0 and svc.snap["latency_p99_ms"] > 0 and svc.snap["error_rate_percent"] == 0.0
        svc.set_fault("bad_config")
        svc.handle("/checkout")
        svc.sample(c, w)
        assert svc.snap["error_rate_percent"] > 0


class TestHttp:
    def test_endpoints_admin_and_bad_input(self, tmp_path):
        s = app.Service("checkout-api", None, tmp_path)
        srv = app.serve(s, "127.0.0.1", 0)
        import threading

        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            assert runner.http("GET", url + "/health")["ok"] is True
            assert runner.http("POST", url + "/admin/fault", {"name": "cpu_loop"})["ok"]
            assert runner.http("GET", url + "/health")["fault"] == "cpu_loop"
            runner.reset_faults(url, url)
            assert runner.http("GET", url + "/health")["fault"] is None
            import urllib.error

            for method, path, body in (
                ("POST", "/admin/fault", {"name": "bogus"}),
                ("POST", "/admin/deploy", {}),
                ("POST", "/nope", {}),
                ("GET", "/nope", None),
            ):
                with pytest.raises(urllib.error.HTTPError):
                    runner.http(method, url + path, body)
            assert runner.http("POST", url + "/admin/deploy", {"version": "9", "description": "d"})["ok"]
            assert runner.http("GET", url + "/deploys")["deploys"][0]["version"] == "9"
            assert "service" in runner.http("GET", url + "/metrics")
        finally:
            srv.shutdown()
            s.stop.set()

    def test_wait_up_fails_fast_on_dead_port(self):
        with pytest.raises(RuntimeError):
            runner.wait_up(f"http://127.0.0.1:{runner.free_port()}", 0.3)


class TestScenarios:
    def test_gold_schema_and_consistency(self):
        assert set(SCENARIOS) == {f"LAB-00{i}" for i in range(1, 6)}
        for s in SCENARIOS.values():
            g = s.gold()
            assert (
                GOLD_KEYS <= set(g)
                and g["remediation_risk_tier"] in {"low", "medium", "high"}
                and len(g["evidence"]) >= 3
            )
            assert (s.fault is None) == s.is_false_alarm and s.target in {"checkout-api", "payments-stub"}
            assert (s.deploy is not None) == s.broken_deploy

    def test_committed_gold_matches_scenarios(self):
        for sid, s in SCENARIOS.items():
            assert json.loads((runner.LAB_DATA / "gold" / f"{sid}.json").read_text()) == s.gold()


class TestCapturedData:
    @pytest.mark.parametrize("sid", sorted(SCENARIOS))
    def test_readable_by_existing_tools(self, sid):
        meta, logs, metrics = read_meta(sid), read_logs(sid), read_metrics(sid)
        assert meta["incident_id"] == sid and "cause" not in json.dumps(meta).lower().replace("never the cause", "")
        assert logs and all(e.service in {"checkout-api", "payments-stub"} for e in logs)
        assert set(metrics) == set(meta["available_metrics"]) and all(len(v) >= 20 for v in metrics.values())
        assert read_deploys(sid)[0]["deploy_id"] == "deploy-000"
        assert (len(find_error_logs(sid)) > 0) == (sid in {"LAB-002", "LAB-003", "LAB-004"})

    def test_signals_match_the_injected_fault(self):
        def peak(sid, m):
            return max(p["value"] for p in read_metrics(sid)[m])

        assert (
            peak("LAB-001", "cpu_percent") > 25
            and peak("LAB-002", "mem_mb") > 60
            and peak("LAB-003", "latency_p99_ms") > 900
        )
        assert peak("LAB-004", "error_rate_percent") > 80 and peak("LAB-005", "error_rate_percent") == 0
        assert peak("LAB-005", "rps") > 2 * 7 and any(d["version"] == "2.4.1" for d in read_deploys("LAB-004"))

    def test_lab_ids_accepted_but_path_syntax_still_refused(self):
        from incident_agent.tools import _incident_dir

        assert _incident_dir("LAB-001").parts[-2:] == ("lab", "LAB-001")
        for bad in ("LAB-../x", "LAB-001/../..", "../LAB-001"):
            with pytest.raises(ValueError):
                _incident_dir(bad)


class TestRunner:
    def test_merge_logs_sorted(self, tmp_path):
        (tmp_path / "a.log").write_text('2026-01-01T00:00:02+00:00 service=a level=INFO msg="x"\n')
        (tmp_path / "b.log").write_text('2026-01-01T00:00:01+00:00 service=b level=INFO msg="y"\n\n')
        out = runner.merge_logs(tmp_path, ["a", "b", "missing"])
        assert [ln.split()[1] for ln in out] == ["service=b", "service=a"]

    def test_end_to_end_real_processes(self, tmp_path):
        d = runner.run_local("LAB-004", out=tmp_path, baseline_s=3, fault_s=7)
        meta = json.loads((d / "meta.json").read_text())
        metrics = json.loads((d / "metrics.json").read_text())
        logs = (d / "logs.log").read_text()
        assert meta["incident_id"] == "LAB-004" and "error_rate_percent" in metrics and len(metrics["cpu_percent"]) >= 4
        assert "KeyError: 'PAYMENT_API_URL'" in logs and "service started" in logs
        assert max(p["value"] for p in metrics["error_rate_percent"]) > 30
        assert json.loads((tmp_path / "gold" / "LAB-004.json").read_text())["category"] == "bad_deploy"
        assert [x["version"] for x in json.loads((d / "deploys.json").read_text())] == ["2.3.0", "2.4.1"]


class TestEvaluate:
    def test_scores_with_the_standard_scorer(self, tmp_path, monkeypatch):
        rep = evaluate.evaluate_lab(diagnose=lambda iid: harness.DiagnosisResult(iid, "Unknown"))
        assert rep["summary"]["n"] == 5 and rep["summary"]["crashed"] == 0 and rep["summary"]["answered_rate"] == 0.0
        assert evaluate.save(rep, tmp_path).name == "lab__react_baseline.json"

    def test_perfect_answer_scores_perfect(self):
        gold = {sid: s.gold() for sid, s in SCENARIOS.items()}

        def oracle(iid):
            g = gold[iid]
            return harness.DiagnosisResult(
                iid,
                g["root_cause"] if not g["is_false_alarm"] else "false alarm: " + g["root_cause"],
                g["evidence"],
                g["remediation_risk_tier"],
            )

        s = evaluate.evaluate_lab(diagnose=oracle)["summary"]
        assert s["risk_accuracy"] == 1.0 and s["false_alarm_accuracy"] == 1.0 and s["evidence_recall"] == 1.0

    def test_unknown_id_and_cli(self, capsys):
        with pytest.raises(ValueError):
            evaluate.evaluate_lab(ids=["LAB-999"])
        assert cli(["list"]) == 0 and "LAB-003" in capsys.readouterr().out
        assert cli(["eval", "--ids", "LAB-001"]) == 0
        assert "f1=" in capsys.readouterr().out
        (Path(harness.RESULTS_DIR) / "lab__react_baseline.json").unlink(missing_ok=True)

    def test_schema_accepts_lab_ids(self):
        from incident_agent.schemas.diagnosis import Diagnosis

        assert "LAB" in Diagnosis.model_fields["incident_id"].metadata[0].pattern


class TestLiveDriver:
    def test_not_running_message(self, monkeypatch, capsys):
        from incident_agent.lab import live

        monkeypatch.setattr(live, "CURL", f"http://127.0.0.1:{runner.free_port()}")
        monkeypatch.setattr(runner, "wait_up", lambda url, s=5.0: (_ for _ in ()).throw(RuntimeError("down")))
        assert live.main(["reset"]) == 2 and "docker compose" in capsys.readouterr().err

    def test_capture_against_running_services(self, tmp_path, monkeypatch):
        import threading

        from incident_agent.lab import live

        pay, chk = app.Service("payments-stub", None, tmp_path / "logs"), None
        ps = app.serve(pay, "127.0.0.1", 0)
        purl = f"http://127.0.0.1:{ps.server_address[1]}"
        chk = app.Service("checkout-api", purl, tmp_path / "logs")
        cs = app.serve(chk, "127.0.0.1", 0)
        curl = f"http://127.0.0.1:{cs.server_address[1]}"
        for srv in (ps, cs):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setattr(live, "CURL", curl)
        monkeypatch.setattr(live, "PURL", purl)
        monkeypatch.setattr(live, "LOG_DIR", tmp_path / "logs")
        gen = runner.LoadGen(curl)
        gen.start()
        try:
            assert live.main(["reset"]) == 0
            assert (
                live.main(["capture", "LAB-005", "--baseline", "2", "--fault", "3", "--out", str(tmp_path / "out")])
                == 0
            )
            d = tmp_path / "out" / "LAB-005"
            assert json.loads((d / "meta.json").read_text())["incident_id"] == "LAB-005"
            assert max(p["value"] for p in json.loads((d / "metrics.json").read_text())["rps"]) > 0
            assert chk.fault is None and pay.fault is None  # faults are always cleared afterwards
        finally:
            gen.close()
            for srv in (ps, cs):
                srv.shutdown()
            pay.stop.set()
            chk.stop.set()
