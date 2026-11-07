"""Append-only audit log of every sandbox decision and execution (JSONL, fsynced).

High-risk actions must be logged with full provenance (``openspec/risk-classification.md``), so the runner treats an
unwritable log as a reason not to run them. The log also remembers consumed approval nonces, so an approval cannot be
replayed after a restart. Command text is redacted and shortened before it is written.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{8,}|(?:api[_-]?key|token|secret|password|passwd|authorization)\s*[=:]\s*\S+"
    r"|\b(?:sk|gsk|ghp|xox[bap])[-_][A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|https?://[^\s/@]+:[^\s/@]+@)"
)


def one_line(text: str, max_len: int = 300) -> str:
    """Redact secret-looking text, drop control characters and shorten."""
    clean = _SECRET.sub("[redacted]", text)
    clean = "".join(c if c.isprintable() else " " for c in clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean if len(clean) <= max_len else clean[: max_len - 1] + "…"


class AuditError(RuntimeError):
    pass


class AuditLog:
    def __init__(self, path: str | Path | None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._nonces: set[str] = set()

    def record(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {"ts": round(self._clock(), 3), "event": event}
        for key, value in fields.items():
            entry[key] = one_line(value) if isinstance(value, str) else value
        if event == "approval_consumed" and "nonce" in entry:
            self._nonces.add(str(entry["nonce"]))
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise AuditError(f"cannot write the audit log: {type(exc).__name__}") from exc

    def entries(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def nonce_used(self, nonce: str) -> bool:
        if nonce in self._nonces:
            return True
        if self.path is None or not self.path.exists():
            return False
        try:
            rows = self.entries()
        except OSError:
            return True  # cannot prove it is unused: treat as used (fail closed)
        return any(r.get("event") == "approval_consumed" and r.get("nonce") == nonce for r in rows)
