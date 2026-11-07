"""Command policy (Stage 16): turn an agent's command line into a decision, without ever running a shell.

``evaluate`` answers three questions: is this one of the allow-listed commands, are its arguments valid, and which
risk tier is it. The answer is a ``Decision``: ``allow`` (read-only, or a dry run), ``needs_approval`` (a mutating
command; high risk, ``openspec/risk-classification.md``) or ``deny`` with a reason the agent can act on.

Nothing here executes anything. Shell syntax is refused rather than interpreted: an agent that writes ``a; b`` or
``$(id)`` believes a shell exists, and the honest answer is "no".
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from typing import Any

from incident_agent.sandbox.commands import BASIC, HIGH, LOW, SPECS, CommandError, parse_tokens

MAX_LINE = 500
MAX_TOKENS = 24
EXPANSION_MARKERS = ("`", "$(", "${")
OPERATOR_CHARS = frozenset("();<>|&")


@dataclass(frozen=True)
class Decision:
    verdict: str  # allow | needs_approval | deny
    reason: str
    command: str | None = None
    tier: str | None = None
    args: dict[str, Any] | None = None
    fingerprint: str | None = None
    requires: frozenset[str] = field(default_factory=lambda: BASIC)

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


def fingerprint(incident_id: str, command: str, args: dict[str, Any]) -> str:
    """Identifies exactly one command on exactly one incident; an approval is bound to this value."""
    canonical = json.dumps(
        {"incident": incident_id, "command": command, "args": args}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:20]


def tokenize(line: str) -> list[str]:
    """Split a command line into arguments. Control characters, expansion and unquoted shell operators are errors."""
    if not isinstance(line, str) or not line.strip():
        raise CommandError("empty command")
    if len(line) > MAX_LINE:
        raise CommandError(f"command longer than {MAX_LINE} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in line):
        raise CommandError("control characters (including newlines) are not allowed; send one command at a time")
    for marker in EXPANSION_MARKERS:
        if marker in line:
            raise CommandError("there is no shell here: command substitution and variable expansion are not supported")
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # '#' must not silently drop the rest of the line
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise CommandError(f"cannot parse the command line ({exc})") from None
    if not tokens:
        raise CommandError("empty command")
    if len(tokens) > MAX_TOKENS:
        raise CommandError(f"more than {MAX_TOKENS} arguments")
    for tok in tokens:
        if tok and all(c in OPERATOR_CHARS for c in tok):
            raise CommandError("there is no shell here: pipes, redirects, ';' and '&' are not supported")
    return tokens


def deny(reason: str, command: str | None = None) -> Decision:
    return Decision("deny", reason, command)


def evaluate(line: str, incident_id: str) -> Decision:
    try:
        tokens = tokenize(line)
    except CommandError as exc:
        return deny(str(exc))
    name, rest = tokens[0], tokens[1:]
    spec = SPECS.get(name)
    if spec is None:
        return deny(f"{name[:40]!r} is not an allow-listed command; available: {', '.join(sorted(SPECS))}")
    try:
        args = parse_tokens(spec, rest)
    except CommandError as exc:
        return deny(str(exc), name)
    fp = fingerprint(incident_id, name, args)
    if spec.tier(args) == HIGH:
        reason = "mutating command: high risk, needs a human approval bound to this exact command (or add --dry-run)"
        return Decision("needs_approval", reason, name, HIGH, args, fp, spec.requires)
    reason = "read-only" if not spec.mutating else "dry run: nothing is changed"
    return Decision("allow", reason, name, LOW, args, fp, BASIC)
