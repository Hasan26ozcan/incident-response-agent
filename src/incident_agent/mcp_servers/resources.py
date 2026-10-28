"""A small TTL + LRU cache for MCP resource text, with explicit stale-entry cleanup.

Entries are keyed by ``(server, uri)``. An entry stops being served when it is older than ``ttl`` seconds, when the
server said the resource changed (``invalidate``), or when the server no longer lists the resource (``keep_only``).
The clock is injectable so tests never sleep.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

Key = tuple[str, str]


@dataclass
class _Entry:
    text: str
    stored_at: float


class ResourceCache:
    def __init__(self, ttl: float = 60.0, max_entries: int = 256, clock: Callable[[], float] | None = None) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[Key, _Entry] = OrderedDict()
        self.hits = self.misses = self.expired = self.invalidated = self.evicted = 0

    def __len__(self) -> int:
        return len(self._entries)

    def keys(self) -> list[Key]:
        return list(self._entries)

    def _is_stale(self, entry: _Entry) -> bool:
        return self._clock() - entry.stored_at >= self.ttl

    def get(self, key: Key) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if self._is_stale(entry):
            del self._entries[key]
            self.expired += 1
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.text

    def put(self, key: Key, text: str) -> None:
        self._entries[key] = _Entry(text, self._clock())
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evicted += 1

    def invalidate(self, key: Key) -> bool:
        if self._entries.pop(key, None) is None:
            return False
        self.invalidated += 1
        return True

    def drop_server(self, server: str) -> int:
        """Forget everything cached for one server (connection closed or lost)."""
        doomed = [k for k in self._entries if k[0] == server]
        for k in doomed:
            del self._entries[k]
        self.invalidated += len(doomed)
        return len(doomed)

    def keep_only(self, server: str, uris: Iterable[str]) -> int:
        """Stale cleanup: drop this server's entries whose uri is not in ``uris`` (no longer listed)."""
        live = set(uris)
        doomed = [k for k in self._entries if k[0] == server and k[1] not in live]
        for k in doomed:
            del self._entries[k]
        self.invalidated += len(doomed)
        return len(doomed)

    def sweep(self) -> int:
        """Remove every expired entry now (``get`` only removes the one it touches)."""
        doomed = [k for k, e in self._entries.items() if self._is_stale(e)]
        for k in doomed:
            del self._entries[k]
        self.expired += len(doomed)
        return len(doomed)
