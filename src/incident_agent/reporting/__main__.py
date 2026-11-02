"""``python -m incident_agent.reporting INC-005 [--diagnosis-json FILE] [--live] [--dir DIR]``

Diagnoses the incident with the offline rule-based agent (no API key) unless a Diagnosis JSON is given, then publishes
the post-mortem and sends the Slack message. Without ``--live`` everything goes to local files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_agent.llm.client import _load_dotenv
from incident_agent.reporting.notify import build_reporter
from incident_agent.reporting.slack import ConfigError
from incident_agent.schemas.diagnosis import Diagnosis


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="incident_agent.reporting", description=__doc__)
    ap.add_argument("incident", help="incident id, for example INC-005")
    ap.add_argument(
        "--diagnosis-json", help="a Diagnosis as JSON (Diagnosis.to_json); default: offline rule-based agent"
    )
    ap.add_argument(
        "--live", action="store_true", help="really post to Slack / publish to Confluence (needs .env settings)"
    )
    ap.add_argument("--dir", help="where local pages, outbox and ledger go (default: REPORT_DIR or .cache/reports)")
    args = ap.parse_args(argv)

    _load_dotenv()
    try:
        if args.diagnosis_json:
            diagnosis = Diagnosis.from_json(Path(args.diagnosis_json).read_text(encoding="utf-8"))
            if diagnosis.incident_id != args.incident:
                print(f"ERROR: the JSON is for {diagnosis.incident_id}, not {args.incident}")
                return 2
        else:
            from incident_agent.agents.react_agent import ReActAgent

            diagnosis = ReActAgent(confidence_threshold=0.7).run(args.incident)
        reporter = build_reporter(live=args.live, base_dir=args.dir)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"mode: {'LIVE' if args.live else 'local files'} ({reporter.describe()})")
    result = reporter.report(diagnosis)
    if result.wiki:
        print(f"wiki:  {result.wiki.action} v{result.wiki.version} {result.wiki.url}")
    if result.wiki_error:
        print(f"wiki:  FAILED {result.wiki_error}")
    if result.slack_sent:
        print("slack: sent")
    elif result.slack_skipped:
        print(f"slack: skipped ({result.slack_skipped})")
    if result.slack_error:
        print(f"slack: FAILED {result.slack_error}")
    for note in result.notes:
        print(f"note:  {note}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
