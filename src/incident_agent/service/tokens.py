# ruff: noqa: E501
"""API tokens for the service: only SHA-256 digests are stored; a token is shown once, when it is created.

Scopes: ``read`` (list/see runs, metrics) and ``submit`` (also start an investigation). Approvals do not use these tokens:
an approver proves who they are with their own roster token (Stage 18), so a service token can never approve anything.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCOPES = ("read", "submit")
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}  # unreadable store: nobody is authenticated (fail closed)
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        with contextlib.suppress(OSError):  # best effort on Windows
            os.chmod(self.path, 0o600)

    def add(self, name: str, scopes: list[str] | tuple[str, ...]) -> str:
        if not NAME_RE.fullmatch(name):
            raise ValueError("name must be 2-32 characters: a letter, then letters, digits, '_' or '-' (lowercase)")
        if not scopes or any(s not in SCOPES for s in scopes):
            raise ValueError(f"scopes must be a non-empty subset of {SCOPES}")
        data = self._load()
        if name in data:
            raise ValueError(f"token {name!r} already exists")
        token = "ira_" + secrets.token_urlsafe(24)
        data[name] = {"digest": _digest(token), "scopes": sorted(set(scopes))}
        self._save(data)
        return token

    def remove(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True

    def names(self) -> dict[str, list[str]]:
        return {n: list(v.get("scopes", [])) for n, v in sorted(self._load().items())}  # type: ignore[call-overload]

    def authenticate(self, token: str) -> Principal | None:
        """Constant-time over every stored token, so timing does not reveal which one was close."""
        found: Principal | None = None
        digest = _digest(token or "")
        for name, row in self._load().items():
            if hmac.compare_digest(str(row.get("digest", "")), digest):
                scopes = frozenset(s for s in row.get("scopes", []) if s in SCOPES)  # type: ignore[attr-defined]
                found = Principal(name, scopes)
        return found
