# ruff: noqa: E501
"""python -m incident_agent.pipeline run LAB-003 --agent llm_react [--cascade] [--window 6000] [--mode shadow|assist]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.eval.harness import ADAPTER_NAMES
from incident_agent.llm.client import LLMConfigError, LLMUnavailable
from incident_agent.pipeline.run import MODES, PipelineConfig, investigate
from incident_agent.pipeline.stack import StackConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incident_agent.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="investigate one incident end to end")
    r.add_argument("incident")
    r.add_argument("--agent", default="llm_react", choices=ADAPTER_NAMES)
    r.add_argument("--mode", default="shadow", choices=MODES)
    r.add_argument("--cascade", action="store_true", help="cheap model first, escalate to the strong one (Groq)")
    r.add_argument("--window", type=int, default=None, help="sliding-window token budget")
    r.add_argument("--no-harden", action="store_true", help="do NOT strip injection-looking lines from tool output")
    r.add_argument("--root", type=Path, default=Path(".cache/pipeline"))
    a = ap.parse_args(argv)
    cfg = PipelineConfig(
        agent=a.agent, mode=a.mode, root=a.root, harden=not a.no_harden, stack=StackConfig(a.cascade, a.window)
    )
    try:
        inv = investigate(a.incident, cfg)
    except (LLMConfigError, LLMUnavailable) as exc:
        print(f"LLM not available: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    d = inv.diagnosis
    print(f"run {inv.run_id}: {inv.status}")
    print(f"diagnosis: [{d.get('category')}] {d.get('service')} - {d.get('root_cause', '')[:200]}")
    print(inv.summary)
    for h in inv.related:
        print(f"related: {h['doc_id']} ({h['type']}) {h['title']}")
    c, ll = inv.observability["cost"], inv.observability["llm"]
    print(
        f"llm calls {ll['calls']}, tokens {ll['prompt_tokens']}/{ll['completion_tokens']} (cached {ll['cached_tokens']}), cost ${c['total_usd']:.6f}, unpriced calls {c['unpriced_calls']}"
    )
    print(f"details: {inv.run_dir}")
    return 0 if inv.status in ("completed", "waiting") else 1


if __name__ == "__main__":
    sys.exit(main())
