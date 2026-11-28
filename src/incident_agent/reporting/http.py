"""Small HTTP client for outbound notifications: no redirects, no secrets in errors, bounded retries.

* Only ``https`` URLs; plain ``http`` is accepted for loopback hosts only (used by the tests and local mocks).
* Redirects are never followed (a redirect could carry credentials to another host).
* 429 and 5xx responses and network errors are retried with ``Retry-After`` / exponential back-off; the final
  response is returned, so the caller decides what a 4xx means.
* Error messages never contain the URL (a Slack webhook URL is a secret) or any header.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_BODY = 1_000_000


class HttpError(RuntimeError):
    """Network-level failure after all retries. Never contains the URL."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    attempts: int = 1


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, *, allowed_hosts: frozenset[str] | None = None) -> str:
    """Return the URL if it is safe to call, else raise ValueError (message without the URL)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("URL must be an http(s) URL with a host")
    if parts.username or parts.password:
        raise ValueError("URL must not contain credentials")
    if parts.scheme == "http" and not is_loopback(host):
        raise ValueError("plain http is only allowed for loopback hosts; use https")
    if allowed_hosts is not None and host not in allowed_hosts and not is_loopback(host):
        raise ValueError(f"host {host!r} is not allowed here (expected one of {', '.join(sorted(allowed_hosts))})")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff: float = 0.5,
        max_retry_after: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.max_retry_after = max_retry_after
        self._sleep = sleep

    def _delay(self, attempt: int, headers: Mapping[str, str]) -> float:
        raw = {k.lower(): v for k, v in headers.items()}.get("retry-after")
        if raw is not None:
            try:
                return float(min(max(float(raw), 0.0), self.max_retry_after))
            except ValueError:
                pass
        return float(min(self.backoff * 2 ** (attempt - 1), self.max_retry_after))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        allowed_hosts: frozenset[str] | None = None,
    ) -> Response:
        check_url(url, allowed_hosts=allowed_hosts)
        loopback = is_loopback(urlsplit(url).hostname or "")
        handlers: list[Any] = [_NoRedirect()]
        if loopback:
            handlers.append(urllib.request.ProxyHandler({}))  # never send local traffic through an env proxy
        opener = urllib.request.build_opener(*handlers)
        last: Response | None = None
        error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))  # noqa: S310
            try:
                with opener.open(req, timeout=self.timeout) as resp:  # noqa: S310
                    last = Response(resp.status, dict(resp.headers), resp.read(MAX_BODY), attempt)
            except urllib.error.HTTPError as exc:
                last = Response(exc.code, dict(exc.headers or {}), exc.read(MAX_BODY), attempt)
            except (urllib.error.URLError, OSError) as exc:  # URLError, timeouts and connection errors are all OSErrors
                reason = getattr(exc, "reason", exc)
                error = type(reason).__name__
                last = None
                if attempt < self.max_attempts:
                    self._sleep(self._delay(attempt, {}))
                continue
            error = None
            if last.status in RETRY_STATUS and attempt < self.max_attempts:
                self._sleep(self._delay(attempt, last.headers))
                continue
            return last
        raise HttpError(f"network error after {self.max_attempts} attempt(s): {error}")
