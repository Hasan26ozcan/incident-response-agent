"""Pre-flight check: verify the provider, key and model capabilities BEFORE spending free-tier quota.

Makes three tiny, uncached calls: plain chat, tool calling, and schema-constrained output. Reports what
works (and whether the provider needed the json_object fallback). Exit code 0 only if all pass.

Usage: python -m incident_agent.eval.preflight [--judge]
"""

from __future__ import annotations

import argparse
import tempfile
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from incident_agent.llm import LLMClient, LLMConfigError, LLMUnavailable, build_client, generate_structured
from incident_agent.llm.client import _load_dotenv
from incident_agent.tools.toolbox import TOOL_SPECS


class _Ping(BaseModel):
    value: int


def _timed(fn: Callable[[], str]) -> tuple[bool, str, float]:
    t0 = time.perf_counter()
    try:
        detail = fn()
        return True, detail, time.perf_counter() - t0
    except (LLMUnavailable, LLMConfigError, ValueError, RuntimeError) as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}", time.perf_counter() - t0


def _unwrap(client: LLMClient) -> list[Any]:
    inner = getattr(client, "inner", client)
    return list(getattr(inner, "clients", [inner]))


def run_checks(client: LLMClient) -> list[tuple[str, bool, str, float]]:
    def chat() -> str:
        r = client.chat([{"role": "user", "content": "Reply with the single word OK."}])
        text = (r.content or "").strip()
        if not text:
            raise ValueError("empty reply")
        return f"reply={text[:30]!r} usage={r.usage}"

    def tools() -> str:
        r = client.chat(
            [{"role": "user", "content": "Call the log_overview tool now. Do not answer in text."}],
            tools=[t for t in TOOL_SPECS if t["function"]["name"] == "log_overview"],
        )
        if not r.tool_calls:
            raise ValueError("model answered in text instead of calling the tool (tool calling unsupported/unreliable)")
        return f"tool_call={r.tool_calls[0].name}"

    def structured() -> str:
        out = generate_structured(
            client, [{"role": "user", "content": "Return JSON with value set to 7."}], _Ping, max_repairs=1
        )
        modes = [("json_object" if getattr(c, "_json_object_mode", False) else "json_schema") for c in _unwrap(client)]
        return f"value={out.value} mode={'/'.join(modes)}"

    results = []
    for name, fn in (("plain chat", chat), ("tool calling", tools), ("structured output", structured)):
        ok, detail, secs = _timed(fn)
        results.append((name, ok, detail, secs))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check provider/model capabilities before an eval run")
    ap.add_argument("--judge", action="store_true", help="also check the LLM-judge model (JUDGE_MODEL)")
    args = ap.parse_args(argv)
    _load_dotenv()
    code = 0
    targets: list[tuple[str, Callable[[], LLMClient]]] = [
        ("agent model", lambda: build_client(cache_dir=tempfile.mkdtemp(prefix="preflight_"))),
    ]
    if args.judge:
        from incident_agent.eval.judge import build_judge_client

        targets.append(("judge model", lambda: build_judge_client()))
    for label, make in targets:
        try:
            client = make()
        except (LLMUnavailable, RuntimeError) as exc:
            print(f"[FAIL] {label}: cannot build client: {exc}")
            code = 1
            continue
        print(f"{label}: {client.model}")
        for name, ok, detail, secs in run_checks(client):
            print(f"  [{'OK' if ok else 'FAIL'}] {name:<18} {secs:5.1f}s  {detail}")
            code |= 0 if ok else 1
    print("READY to run the eval." if code == 0 else "NOT READY: fix the FAIL lines above before running the eval.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
