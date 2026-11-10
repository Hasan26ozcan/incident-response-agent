"""Who may approve (Stage 18): names, roles and a secret token each.

Only a SHA-256 digest of each token is stored (tokens are 192 random bits, so a fast hash is enough). ``add`` returns
the token once, to the person who will use it; it is never written anywhere in clear. Approvers present the token
through the environment (``INCIDENT_APPROVER_TOKEN``), not as an argument that lands in shell history.

This is *local* authentication for a local tool: whoever can read the roster file and guess nothing still cannot
approve, but whoever holds a token is that person. Binding approvals to a real identity provider (SSO, signed
requests) is outside this stage and is said so in the docs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from incident_agent.hitl.policy import NAME_RE


class AuthError(PermissionError):
    """Unknown approver or wrong token. The message never says which."""


@dataclass(frozen=True)
class Approver:
    name: str
    roles: frozenset[str]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Roster:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise AuthError(f"the roster cannot be read ({type(exc).__name__})") from None
        if not isinstance(data, dict):
            raise AuthError("the roster file is not an object")
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _save(self, data: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".roster-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def add(self, name: str, roles: list[str] | tuple[str, ...]) -> str:
        """Create or re-key an approver. Returns the new token (shown once)."""
        if not NAME_RE.fullmatch(name):
            raise ValueError("approver name must be 1-24 characters of a-z, 0-9, '.', '_' or '-'")
        if not roles or any(not NAME_RE.fullmatch(r) for r in roles):
            raise ValueError("an approver needs one or more valid roles")
        token = secrets.token_urlsafe(24)
        data = self._load()
        data[name] = {"roles": sorted(set(roles)), "token_sha256": _digest(token)}
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
        return {n: [str(r) for r in v.get("roles", [])] for n, v in sorted(self._load().items())}  # type: ignore[attr-defined]

    def authenticate(self, name: str, token: str) -> Approver:
        entry = self._load().get(name) if isinstance(name, str) else None
        stored = str(entry.get("token_sha256", "")) if entry else ""
        # compare even for an unknown name so the answer takes the same time
        ok = hmac.compare_digest(_digest(token if isinstance(token, str) else ""), stored or "0" * 64)
        if not entry or not stored or not ok:
            raise AuthError("unknown approver or wrong token")
        roles = entry.get("roles")
        if not isinstance(roles, list):
            raise AuthError("unknown approver or wrong token")
        return Approver(name, frozenset(str(r) for r in roles))
