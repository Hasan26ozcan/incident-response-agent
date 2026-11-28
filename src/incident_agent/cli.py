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
from incident_agent.schemas.command_diagnosis import CommanderDiagnosis
from incident_agent.schemas.diagnosis import Diagnosis


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
        print(
            "Usage: incident-agent diagnose <INC-ID> "
            "[--agent llm|orchestrator|debate|tot|swarm|swarm-debate|swarm-full|legacy] [--risk-guard] "
            "[--report | --report-live]"
        )
        print("Example: incident-agent diagnose INC-001")
        return 1

    _load_dotenv()
    incident_id = args[0]
    mode = "llm" if os.environ.get("GROQ_API_KEY") else "legacy"
    if "--agent" in args:
        mode = args[args.index("--agent") + 1]
    guard = "--risk-guard" in args
    diagnosis: Diagnosis | CommanderDiagnosis
    if mode in ("llm", "orchestrator", "debate", "tot", "swarm", "swarm-debate", "swarm-full"):
        from incident_agent.llm import build_client

        try:
            client = build_client()
            if mode == "llm":
                from incident_agent.agents.llm_react_agent import LLMReActAgent

                diagnosis = LLMReActAgent(client, risk_guard=guard).run(incident_id)
            elif mode == "orchestrator":
                from incident_agent.agents.llm_orchestrator import LLMOrchestrator

                diagnosis = LLMOrchestrator(client, risk_guard=guard).run(incident_id)
            elif mode == "tot":
                from incident_agent.agents.tree_of_thought import ToTOrchestrator

                diagnosis = ToTOrchestrator(client, risk_guard=guard).run(incident_id)
            elif mode == "debate":
                from incident_agent.agents.llm_debate import DebatedOrchestrator

                diagnosis = DebatedOrchestrator(client, risk_guard=guard).run(incident_id)
            else:
                from incident_agent.agents.swarm import HierarchicalSwarm

                diagnosis = HierarchicalSwarm(
                    client, risk_guard=guard, debate=mode in ("swarm-debate", "swarm-full"), tot=mode == "swarm-full"
                ).run(incident_id)
        except LLMUnavailable as exc:
            print(f"LLM unavailable: {exc}", file=sys.stderr)
            return 1
    else:
        diagnosis = ReActAgent(confidence_threshold=0.7).run(incident_id)
    print(diagnosis.format_report())
    if "--report" in args or "--report-live" in args:
        return _report(diagnosis, live="--report-live" in args)
    return 0


def _report(diagnosis: Diagnosis | CommanderDiagnosis, *, live: bool) -> int:
    """Stage 15: publish the post-mortem and notify Slack (local files unless ``--report-live``)."""
    from incident_agent.reporting import ConfigError, build_reporter

    if isinstance(diagnosis, CommanderDiagnosis):
        diagnosis = (
            diagnosis.diagnosis
        )  # the commander wraps the technical diagnosis; the post-mortem is built from that
    try:
        reporter = build_reporter(live=live)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result = reporter.report(diagnosis)
    print(f"\nreport ({'LIVE' if live else 'local files'}: {reporter.describe()})")
    if result.wiki:
        print(f"  wiki:  {result.wiki.action} {result.wiki.url}")
    if result.wiki_error:
        print(f"  wiki:  FAILED {result.wiki_error}")
    if result.slack_sent:
        print("  slack: sent")
    elif result.slack_skipped:
        print(f"  slack: skipped ({result.slack_skipped})")
    if result.slack_error:
        print(f"  slack: FAILED {result.slack_error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
