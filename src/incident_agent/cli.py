"""Command-line entry point.

At Stage 2 this only proves the packaging (`incident-agent` console script)
and CI wiring work end to end. Stage 3 replaces the body of `main()` with
the single-agent ReAct loop described in ROADMAP.md Phase B, pointed at the
synthetic incidents under data/incidents/ (see data/README.md for the
schema and eval/rubric.md for how its output will be scored).
"""

from __future__ import annotations

import sys

from incident_agent import __version__


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `incident-agent` console script.

    Currently a placeholder that prints its version and exits 0, so CI has
    something real to install and invoke. Returns a process exit code
    rather than calling sys.exit() directly, so it stays testable.
    """
    del argv  # unused until Stage 3 adds real argument parsing
    print(f"incident-agent v{__version__} — skeleton only. The ReAct diagnosis loop lands in Stage 3 (see ROADMAP.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
