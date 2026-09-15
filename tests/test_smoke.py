"""Stage 2 smoke tests.

These exist to give CI something real to run green against — proving the
package installs, imports, and its console-script entry point works —
before any actual agent logic exists. Stage 3+ will add real behavioral
tests alongside the agents they cover; this file should stay small.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

import incident_agent
from incident_agent.cli import main


def test_version_is_semver() -> None:
    assert re.match(r"^\d+\.\d+\.\d+$", incident_agent.__version__)


def test_cli_main_returns_zero() -> None:
    exit_code = main([])
    assert exit_code == 0


def test_cli_main_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    main([])
    captured = capsys.readouterr()
    assert incident_agent.__version__ in captured.out
    assert "incident-agent" in captured.out


def test_console_script_installed() -> None:
    """Confirms `pip install -e .` actually registered the entry point."""
    result = subprocess.run(
        [sys.executable, "-m", "incident_agent.cli"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert "incident-agent" in result.stdout


def test_all_subpackages_import() -> None:
    """Every Stage-3+ subpackage should at least import cleanly, even empty."""
    import importlib

    for subpackage in ["agents", "tools", "memory", "workflows", "eval"]:
        importlib.import_module(f"incident_agent.{subpackage}")
