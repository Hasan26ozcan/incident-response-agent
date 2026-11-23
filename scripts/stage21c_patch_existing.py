# ruff: noqa: E501
"""Idempotent edits to earlier-stage files for the end-to-end pipeline (run once from the project root).

* eval/harness.py        - ``use_client`` / ``shared_client``: the llm_* adapters can share an injected client
* agents/tool_loop.py    - ``hardened()`` switch: injection-looking lines are removed from tool output (off by default)
* durable/workflows.py   - ``Deps.diagnoser``: a real agent can replace the keyword rules in the ``diagnose`` step
* tools/toolbox.py, agents/worker.py - resolve incident folders through ``tools._incident_dir`` (LAB ids)
* prompts/builder.py     - ``build_prompt(..., harden=False)``
A file that no longer matches is reported and left alone (nothing is guessed).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "incident_agent"

HARNESS_HELPER = '''_CLIENT_OVERRIDE: Any = None


@contextmanager
def use_client(client: Any) -> Iterator[None]:
    """Make the llm_* adapter factories built inside this block share ``client`` (tracing, cascade, windowing...)."""
    global _CLIENT_OVERRIDE
    previous, _CLIENT_OVERRIDE = _CLIENT_OVERRIDE, client
    try:
        yield
    finally:
        _CLIENT_OVERRIDE = previous


def shared_client() -> Any:
    if _CLIENT_OVERRIDE is not None:
        return _CLIENT_OVERRIDE
    from incident_agent.llm import build_client

    return build_client()


'''

TOOL_LOOP_OLD = '''

def wrap_output(text: str) -> str:
    """Mark tool output as untrusted data; neutralise a forged closing tag inside it."""
    return'''
TOOL_LOOP_NEW = '''
HARDEN_TOOL_OUTPUT = False  # Stage 21c: when True, lines that look like prompt injection are removed from tool output


@contextmanager
def hardened(enabled: bool = True) -> Iterator[None]:
    """Turn tool-output hardening on for the code run inside the block (off by default, so behaviour is unchanged)."""
    global HARDEN_TOOL_OUTPUT
    previous, HARDEN_TOOL_OUTPUT = HARDEN_TOOL_OUTPUT, enabled
    try:
        yield
    finally:
        HARDEN_TOOL_OUTPUT = previous


def wrap_output(text: str) -> str:
    """Mark tool output as untrusted data; neutralise a forged closing tag inside it."""
    if HARDEN_TOOL_OUTPUT:
        from incident_agent.security.injection import neutralise

        text = neutralise(text)
    return'''

BUILDER_LOGS_OLD = "    logs_text = _format_logs(error_logs)\n"
BUILDER_LOGS_NEW = """    if harden:
        from incident_agent.security.injection import harden as harden_lines

        logs_text = harden_lines(error_logs, "error_logs")
    else:
        logs_text = _format_logs(error_logs)
"""

# (relative path, done marker, [(old, new, count)]); count 0 = all occurrences
PATCHES = [
    (
        "eval/harness.py",
        "def shared_client",
        [
            (
                "from collections.abc import Callable\n",
                "from collections.abc import Callable, Iterator\nfrom contextlib import contextmanager\n",
                1,
            ),
            (
                "from incident_agent.llm import TrackingClient, build_client\n",
                "from incident_agent.llm import TrackingClient\n",
                0,
            ),
            ("shared = build_client()", "shared = shared_client()", 0),
            ("@dataclass\nclass DiagnosisResult", HARNESS_HELPER + "@dataclass\nclass DiagnosisResult", 1),
        ],
    ),
    (
        "agents/tool_loop.py",
        "def hardened",
        [
            (
                "from collections.abc import Callable\n",
                "from collections.abc import Callable, Iterator\nfrom contextlib import contextmanager\n",
                1,
            ),
            (TOOL_LOOP_OLD, TOOL_LOOP_NEW, 1),
        ],
    ),
    (
        "durable/workflows.py",
        "diagnoser: Callable",
        [
            (
                "    allow_unsigned: bool = False  # INSECURE, tests/demos only: accept an unsealed 'approval' signal\n",
                "    allow_unsigned: bool = False  # INSECURE, tests/demos only: accept an unsealed 'approval' signal\n"
                "    # Stage 21c: (incident_id, triage) -> diagnosis; replaces the keyword rules when set\n"
                "    diagnoser: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None\n",
                1,
            ),
            (
                '        triage = ctx.results["triage"]\n        data = _load(deps, triage["incident_id"])',
                '        triage = ctx.results["triage"]\n        if deps.diagnoser is not None:\n            return deps.diagnoser(triage["incident_id"], triage)\n        data = _load(deps, triage["incident_id"])',
                1,
            ),
        ],
    ),
    (
        "tools/toolbox.py",
        "_incident_dir(incident_id).is_dir()",
        [
            (
                'if not (PROJECT_ROOT / "data" / "incidents" / incident_id).is_dir():',
                "if not _incident_dir(incident_id).is_dir():",
                1,
            ),
            (
                "from incident_agent.tools import PROJECT_ROOT, read_deploys",
                "from incident_agent.tools import _incident_dir, read_deploys",
                1,
            ),
        ],
    ),
    (
        "agents/worker.py",
        "_incident_dir(self.incident_id)",
        [
            (
                '        path = ROOT / "data" / "incidents" / self.incident_id / "meta.json"\n        return json.loads(path.read_text())',
                '        from incident_agent.tools import _incident_dir\n\n        return json.loads((_incident_dir(self.incident_id) / "meta.json").read_text())',
                1,
            )
        ],
    ),
    (
        "prompts/builder.py",
        "harden: bool = False",
        [
            (
                "    examples: list[dict] | None = None,\n) -> str:",
                "    examples: list[dict] | None = None,\n    harden: bool = False,\n) -> str:",
                1,
            ),
            (
                "        examples: Override few-shot examples (defaults to FEW_SHOT_EXAMPLES).\n",
                "        examples: Override few-shot examples (defaults to FEW_SHOT_EXAMPLES).\n"
                "        harden: Stage 21c, opt-in. Remove log lines that look like prompt injection and fence the rest as data.\n",
                1,
            ),
            (BUILDER_LOGS_OLD, BUILDER_LOGS_NEW, 1),
        ],
    ),
]


def main() -> None:
    for rel, done, edits in PATCHES:
        path = SRC / rel
        if not path.exists():
            print(f"skip {rel}: not found")
            continue
        text = path.read_text(encoding="utf-8")
        if done in text:
            print(f"skip {rel}: already done")
            continue
        new = text
        ok = True
        for old, repl, count in edits:
            if old not in new:
                ok = False
                break
            new = new.replace(old, repl) if count == 0 else new.replace(old, repl, count)
        if not ok:
            print(f"WARNING {rel}: expected text not found, left unchanged (see docs/STAGE21C_PIPELINE.md)")
            continue
        path.write_text(new, encoding="utf-8", newline="")
        print(f"updated {rel}")


main()
