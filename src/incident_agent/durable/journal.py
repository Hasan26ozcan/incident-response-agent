"""Durable storage for one workflow run (Stage 17): an append-only journal, a signal inbox and a run lock.

Everything a run needs to resume lives in files under one directory:

* ``journal.jsonl`` - ordered events, one JSON object per line, ``fsync``-ed on every append. A crash can only ever
  damage the *last* line; a torn tail is ignored on read and truncated before the next append. A damaged line in the
  middle is corruption and refuses to load (never guess).
* ``signals.jsonl`` - inbox other processes append to while a worker owns the run (small atomic ``O_APPEND`` writes).
* ``lock.json``     - diagnostic owner metadata; Windows uses a named OS mutex keyed to the run directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_SIGNAL_BYTES = 2048


class JournalError(RuntimeError):
    """The journal cannot be trusted (corruption) or cannot be written."""


class LockError(RuntimeError):
    """Another live worker owns this run."""


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id) or run_id.endswith("."):
        raise ValueError("run id must be 1-64 characters of letters, digits, '.', '_' or '-'")
    return run_id


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - directories cannot be opened on Windows
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    def __init__(self, run_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(run_dir)
        self.path = self.dir / "journal.jsonl"
        self._clock = clock

    def exists(self) -> bool:
        return self.path.exists()

    def _read(self) -> tuple[list[dict[str, Any]], int]:
        """Return (events, byte length of the valid prefix)."""
        if not self.path.exists():
            return [], 0
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise JournalError(f"cannot read the journal: {type(exc).__name__}") from exc
        events: list[dict[str, Any]] = []
        good = 0
        pos = 0
        while pos < len(raw):
            nl = raw.find(b"\n", pos)
            complete = nl != -1
            line = raw[pos : nl if complete else len(raw)]
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("seq") != len(events) + 1:
                    raise ValueError("bad event")
            except ValueError:
                if complete and nl + 1 == len(raw):
                    break  # a bad last line that did get its newline: still a torn write
                if not complete:
                    break  # torn tail
                raise JournalError(f"journal is corrupt at event {len(events) + 1}") from None
            if not complete:
                break  # valid JSON but no newline: the append was cut short, treat as torn
            events.append(event)
            pos = nl + 1
            good = pos
        return events, good

    def events(self) -> list[dict[str, Any]]:
        return self._read()[0]

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        events, good = self._read()
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size != good:
            with self.path.open("r+b") as fh:  # drop a torn tail so the next line starts clean
                fh.truncate(good)
                fh.flush()
                os.fsync(fh.fileno())
        event = {"seq": len(events) + 1, "ts": round(self._clock(), 3), "kind": kind, **fields}
        line = (json.dumps(event, sort_keys=True) + "\n").encode()
        fresh = not self.path.exists()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            if fresh:
                _fsync_dir(self.dir)
        except OSError as exc:
            raise JournalError(f"cannot write the journal: {type(exc).__name__}") from exc
        return event


# --- signals -------------------------------------------------------------------------------------------------------


def write_signal(run_dir: Path, name: str, payload: dict[str, Any] | None = None) -> str:
    """Append one signal to the inbox (safe from any process, also while a worker runs the workflow)."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name):
        raise ValueError("signal name must be lowercase letters, digits and '_' (max 41 characters)")
    sid = uuid.uuid4().hex
    data = (json.dumps({"id": sid, "name": name, "payload": payload or {}}, sort_keys=True) + "\n").encode()
    if len(data) > MAX_SIGNAL_BYTES:
        raise ValueError(f"signal larger than {MAX_SIGNAL_BYTES} bytes")
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(run_dir / "signals.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return sid


def read_signals(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "signals.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn or garbage line is never a signal
        if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("name"), str):
            out.append({"id": row["id"], "name": row["name"], "payload": row.get("payload") or {}})
    return out


# --- lock ----------------------------------------------------------------------------------------------------------


class RunLock:
    """An exclusive OS lock on one run. ``acquire`` fails fast while a live worker holds it.

    The operating system releases the lock when its owner dies (also on SIGKILL), so there is no lease to expire and
    no stale-lock race: two workers can never run the same workflow at the same time.
    """

    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)
        self.path = self.dir / "lock.json"
        self.lock_path = self.dir / "lock.json.lock"
        self._fd: int | None = None
        self._win_thread: threading.Thread | None = None
        self._win_release: threading.Event | None = None
        self._win_error: list[int] = []

    def acquire(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
            import ctypes
            from ctypes import wintypes

            key = hashlib.sha256(str(self.lock_path.resolve()).casefold().encode()).hexdigest()
            name = "Local\\incident-agent-" + key
            ready, release = threading.Event(), threading.Event()
            failure: list[int] = []

            def hold_mutex() -> None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                create_mutex = kernel32.CreateMutexW
                create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                create_mutex.restype = wintypes.HANDLE
                wait = kernel32.WaitForSingleObject
                wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                wait.restype = wintypes.DWORD
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = [wintypes.HANDLE]
                close_handle.restype = wintypes.BOOL
                handle = create_mutex(None, False, name)
                if not handle:
                    failure.append(ctypes.get_last_error())
                    ready.set()
                    return
                status = wait(handle, 0)
                if status not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED
                    failure.append(258 if status == 258 else ctypes.get_last_error())
                    close_handle(handle)
                    ready.set()
                    return
                ready.set()
                release.wait()
                release_mutex = kernel32.ReleaseMutex
                release_mutex.argtypes = [wintypes.HANDLE]
                release_mutex.restype = wintypes.BOOL
                if not release_mutex(handle):
                    failure.append(ctypes.get_last_error())
                if not close_handle(handle):
                    failure.append(ctypes.get_last_error())

            thread = threading.Thread(target=hold_mutex, daemon=True)
            thread.start()
            ready.wait()
            if failure:
                thread.join()
                if failure[0] == 258:
                    raise LockError("another worker is running this workflow") from None
                raise ctypes.WinError(failure[0])
            self._win_thread, self._win_release, self._win_error = thread, release, failure
        else:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            except OSError:
                os.close(fd)
                raise LockError("another worker is running this workflow") from None
            self._fd = fd
        info = json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode()
        try:
            metadata_fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(metadata_fd, info)
                os.fsync(metadata_fd)
            finally:
                os.close(metadata_fd)
        except OSError:
            self.release()
            raise

    def release(self) -> None:
        if self._win_thread is not None:  # pragma: no cover - exercised on Windows only
            thread, self._win_thread = self._win_thread, None
            release, self._win_release = self._win_release, None
            self._win_error.clear()
            if release is not None:
                release.set()
            thread.join()
            if self._win_error:
                import ctypes

                raise ctypes.WinError(self._win_error[0])  # type: ignore[attr-defined,unused-ignore]
            return
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined,unused-ignore]
        finally:
            os.close(fd)
