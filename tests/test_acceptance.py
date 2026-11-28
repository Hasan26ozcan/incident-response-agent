"""Stage 21g: whole-system acceptance chain (quick form; slow steps: python -m incident_agent.acceptance)."""

from incident_agent import acceptance


def test_quick_acceptance_passes_end_to_end():
    res = acceptance.run(quick=True)
    failed = [(r["name"], r["detail"]) for r in res if r["status"] == "FAIL"]
    assert not failed, failed
    ran = {r["name"] for r in res if r["status"] == "ok"}
    assert {"imports", "data", "pipeline", "security", "service-shadow", "service-assist", "measure", "no-leaks"} <= ran


def test_the_leak_scan_can_fail(tmp_path):
    ctx = acceptance.Ctx(tmp_path, True)
    for n in range(25):
        (tmp_path / f"f{n}.txt").write_text("clean")
    assert acceptance.s_leaks(ctx).startswith("25 files")
    (tmp_path / "f3.txt").write_text(f"oops {acceptance.PLANTED}")
    try:
        acceptance.s_leaks(ctx)
    except AssertionError as exc:
        assert "f3.txt" in str(exc)
    else:
        raise AssertionError("a planted secret was not detected")
    ctx2 = acceptance.Ctx(tmp_path, True)
    ctx2.responses.append(f"x {acceptance.PLANTED}")
    try:
        acceptance.s_leaks(ctx2)
    except AssertionError:
        return
    raise AssertionError("a leaking response was not detected")


def test_main_exit_codes(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert acceptance.main(["--quick", "--only", "imports,data", "--json", str(out)]) == 0
    assert out.exists() and "ACCEPTANCE PASS" in capsys.readouterr().out


def test_the_environment_is_restored(monkeypatch):
    import os

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    acceptance.run(quick=True, only=["imports"])
    assert "GROQ_API_KEY" not in os.environ
    monkeypatch.setenv("GROQ_API_KEY", "mine")
    acceptance.run(quick=True, only=["imports"])
    assert os.environ["GROQ_API_KEY"] == "mine"
