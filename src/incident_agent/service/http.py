# ruff: noqa: E501
"""HTTP layer (stdlib). Loopback by default, bearer-token auth, size-limited JSON bodies, no secret ever in a response."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from incident_agent.service.core import Service
from incident_agent.service.dashboard import PAGE
from incident_agent.service.tokens import Principal, TokenStore

MAX_BODY = 8192
AUTH_FAILS_PER_MIN = 10
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
}
RUN = re.compile(r"/api/runs/([a-z0-9-]{1,40})")
DECIDE = re.compile(r"/api/requests/([A-Za-z0-9._-]{1,80})/(approve|reject)")


class _Throttle:
    """Too many failed logins from one address -> 429, so a token cannot be guessed at speed."""

    def __init__(self) -> None:
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, who: str) -> bool:
        with self._lock:
            q = self._fails.setdefault(who, deque())
            while q and q[0] < time.monotonic() - 60:
                q.popleft()
            return len(q) >= AUTH_FAILS_PER_MIN

    def fail(self, who: str) -> None:
        with self._lock:
            self._fails.setdefault(who, deque()).append(time.monotonic())


def make_server(service: Service, tokens: TokenStore, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    throttle = _Throttle()

    class Handler(BaseHTTPRequestHandler):
        server_version = "incident-agent"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # never log headers or bodies
            pass

        # -------------------------------------------------------------------------------- helpers
        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _auth(self, scope: str) -> Principal | None:
            who = self.client_address[0]
            if throttle.blocked(who):
                self._send(429, {"error": "too many failed attempts"})
                return None
            head = self.headers.get("Authorization", "")
            p = tokens.authenticate(head[7:].strip()) if head.lower().startswith("bearer ") else None
            if p is None or scope not in p.scopes:
                throttle.fail(who)
                service.counters["auth_failed"] += 1
                self._send(401 if p is None else 403, {"error": "unauthorized" if p is None else "missing scope"})
                return None
            return p

        def _json_body(self) -> dict[str, Any] | None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                self._send(413 if n > MAX_BODY else 400, {"error": "body must be 1-8192 bytes of JSON"})
                return None
            if "application/json" not in self.headers.get("Content-Type", ""):
                self._send(415, {"error": "Content-Type must be application/json"})
                return None
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                self._send(400, {"error": "invalid JSON"})
                return None
            if not isinstance(data, dict):
                self._send(400, {"error": "JSON object expected"})
                return None
            return data

        # -------------------------------------------------------------------------------- routes
        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path in ("/", "/dashboard"):
                return self._send(200, PAGE, "text/html")
            if path == "/healthz":
                return self._send(200, service.health())
            if path == "/readyz":
                code, body = service.ready()
                return self._send(code, body)
            if self._auth("read") is None:
                return None
            if path == "/metrics":
                return self._send(200, service.metrics(), "text/plain")
            if path == "/api/runs":
                return self._send(200, service.list_runs())
            if path == "/api/requests":
                code, body = service.list_requests()
                return self._send(code, body)
            m = RUN.fullmatch(path)
            if m:
                code, body = service.get_run(m.group(1))
                return self._send(code, body)
            return self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/api/incidents":
                p = self._auth("submit")
                if p is None:
                    return None
                data = self._json_body()
                if data is None:
                    return None
                code, body = service.submit(data.get("incident_id"), p.name)
                return self._send(code, body)
            m = DECIDE.fullmatch(path)
            if m:
                # approvers authenticate with their own roster token, never with a service token
                name, token = self.headers.get("X-Approver", ""), self.headers.get("X-Approver-Token", "")
                if throttle.blocked(self.client_address[0]):
                    return self._send(429, {"error": "too many failed attempts"})
                data = self._json_body()
                if data is None:
                    return None
                code, body = service.decide(
                    m.group(1),
                    m.group(2) == "approve",
                    name,
                    token,
                    str(data.get("confirm", "")),
                    str(data.get("comment", "")),
                )
                if code == 401:
                    throttle.fail(self.client_address[0])
                return self._send(code, body)
            return self._send(404, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802
            self._send(405, {"error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT  # noqa: N815

    return ThreadingHTTPServer((host, port), Handler)
