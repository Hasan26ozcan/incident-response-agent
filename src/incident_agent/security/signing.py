"""Signed approvals (Stage 20): only the approval gate can tell a run that a human approved.

Before this stage the durable ``approval`` wait accepted *any* signal written to the run directory, so a local actor
with write access to it could approve a mutating command without the gate. Now the controller (the gate's delivery
arm) seals every decision with an HMAC over ``(run id, plan fingerprint, decision)`` and ``execute`` verifies the seal:

* a signal written by anyone who does not hold the key is refused (the command only dry-runs),
* a valid seal cannot be replayed into another run or for another plan (both are inside the MAC),
* editing ``approved`` / ``by`` in transit invalidates the seal.

The key lives next to the gate state (``signing.key``, mode 0600), never in a run directory. **Limit:** HMAC is
symmetric, so a same-user attacker who can read the key can still forge. Separating the gate and the worker into
different OS users (or moving to asymmetric signatures) is the real fix; this closes the unprivileged-write hole.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

KEY_BYTES = 32
VERSION = 1


class SigningError(RuntimeError):
    pass


def load_or_create_key(path: Path) -> bytes:
    """Read the key, creating a random one (0600) on first use. A key that others can read is refused (POSIX)."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # lost a race with another process: use its key
            return load_or_create_key(path)
        try:
            os.write(fd, secrets.token_hex(KEY_BYTES).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SigningError(f"{path} is readable by other users; chmod 600 it (or delete it to create a new key)")
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        raise SigningError(f"{path} is not a valid signing key") from None
    if len(key) < KEY_BYTES:
        raise SigningError(f"{path} holds a key shorter than {KEY_BYTES} bytes")
    return key


class ApprovalSigner:
    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < KEY_BYTES:
            raise SigningError(f"the signing key must be at least {KEY_BYTES} bytes")
        self._key = key

    @classmethod
    def from_file(cls, path: Path) -> ApprovalSigner:
        return cls(load_or_create_key(path))

    @staticmethod
    def _message(run_id: str, fingerprint: str, payload: dict[str, Any]) -> bytes:
        body = {
            "v": VERSION,
            "run": run_id,
            "fingerprint": fingerprint,
            "approved": payload.get("approved") is True,
            "by": payload.get("by") if isinstance(payload.get("by"), str) else "",
            "request": payload.get("request") if isinstance(payload.get("request"), str) else "",
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, run_id: str, fingerprint: str, payload: dict[str, Any]) -> str:
        return hmac.new(self._key, self._message(run_id, fingerprint, payload), hashlib.sha256).hexdigest()

    def seal(self, run_id: str, fingerprint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "sig": self.sign(run_id, fingerprint, payload)}

    def verify(self, run_id: str, fingerprint: str, payload: dict[str, Any]) -> bool:
        sig = payload.get("sig")
        if not isinstance(sig, str) or not sig or not run_id or not fingerprint:
            return False
        return hmac.compare_digest(sig, self.sign(run_id, fingerprint, payload))
