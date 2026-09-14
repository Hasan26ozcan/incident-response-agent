"""Shared test fixtures and configuration for the incident-response-agent test suite."""
import sys
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
