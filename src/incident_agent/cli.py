"""Command-line entry point.

At Stage 3 this implements the `diagnose` subcommand that runs
the single-agent ReAct loop against a synthetic incident.
See ROADMAP.md Phase B for the single-agent ReAct skeleton spec.
"""

from __future__ import annotations

import os
import sys

from incident_agent import __version__
from incident_agent.agents.react_agent import ReActAgent
from incident_agent.llm.client import LLMUnavailable, _load_dotenv


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `incident-agent` console script."""
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        print(f"incident-agent v{__version__} — use 'diagnose <INC-ID>'")
        return 0

    command = args[0]

    if command == "diagnose":
        return _cmd_diagnose(args[1:])

    print(f"Unknown command: {command}")
    return 1


def _cmd_diagnose(args: list[str]) -> int:
    """Run the ReAct agent against a single incident."""
    if not args:
        print("Usage: incident-agent diagnose <INC-ID> [--agent llm|legacy]")
        print("Example: incident-agent diagnose INC-001")
        return 1

    _load_dotenv()
    incident_id = args[0]
    mode = "llm" if os.environ.get("GROQ_API_KEY") else "legacy"
    if "--agent" in args:
        mode = args[args.index("--agent") + 1]
    if mode == "llm":
        from incident_agent.agents.llm_react_agent import LLMReActAgent
        from incident_agent.llm import build_client

        try:
            diagnosis = LLMReActAgent(build_client()).run(incident_id)
        except LLMUnavailable as exc:
            print(f"LLM unavailable: {exc}", file=sys.stderr)
            return 1
    else:
        diagnosis = ReActAgent(confidence_threshold=0.7).run(incident_id)
    print(diagnosis.format_report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
