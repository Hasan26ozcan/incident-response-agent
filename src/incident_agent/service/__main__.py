"""python -m incident_agent.service serve | token add/list/remove | roster add/list | selfcheck"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from incident_agent.service.core import Service, ServiceConfig
from incident_agent.service.http import make_server
from incident_agent.service.tokens import SCOPES, TokenStore

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _root(a: argparse.Namespace) -> Path:
    return Path(a.root or os.environ.get("INCIDENT_SERVICE_DIR", ".cache/service"))


def _sender() -> object | None:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return None
    from incident_agent.reporting.slack import WebhookSender

    return WebhookSender(url)  # raises ConfigError (without the URL) if it is not a Slack https URL


def cmd_serve(a: argparse.Namespace) -> int:
    if a.host not in LOOPBACK and not a.allow_remote:
        print(
            "refusing to listen on a non-loopback address without --allow-remote (put TLS in front of it)",
            file=sys.stderr,
        )
        return 2
    root = _root(a)
    tokens = TokenStore(root / "tokens.json")
    if not tokens.names():
        print(
            "no API tokens yet: run  python -m incident_agent.service token add NAME --scopes read,submit",
            file=sys.stderr,
        )
        return 2
    from incident_agent.eval.harness import ADAPTER_NAMES

    if a.agent not in ADAPTER_NAMES:
        print(f"unknown agent {a.agent!r}; choose from {list(ADAPTER_NAMES)}", file=sys.stderr)
        return 2
    client_factory = None
    if a.agent != "react_baseline":  # the rule-based baseline needs no model
        from incident_agent.eval.harness import shared_client

        client_factory = shared_client
    svc = Service(
        ServiceConfig(
            root=root, mode=a.mode, agent=a.agent, cascade=a.cascade, client_factory=client_factory, sender=_sender()
        )
    )
    svc.start()
    srv = make_server(svc, tokens, a.host, a.port)
    print(f"listening on http://{a.host}:{a.port}  mode={a.mode}  agent={a.agent}  (Ctrl+C stops)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        svc.stop()
    return 0


def cmd_token(a: argparse.Namespace) -> int:
    ts = TokenStore(_root(a) / "tokens.json")
    if a.action == "add":
        scopes = [s for s in a.scopes.split(",") if s]
        print(f"token for {a.name} (shown once, store it safely):\n{ts.add(a.name, scopes)}")
    elif a.action == "remove":
        print("removed" if ts.remove(a.name) else "no such token")
    else:
        for n, s in ts.names().items():
            print(n, ",".join(s))
    return 0


def cmd_roster(a: argparse.Namespace) -> int:
    from incident_agent.hitl.roster import Roster

    r = Roster(_root(a) / "roster.json")
    if a.action == "add":
        print(f"approver token for {a.name} (shown once):\n{r.add(a.name, [x for x in a.roles.split(',') if x])}")
    else:
        for n, roles in r.names().items():
            print(n, ",".join(roles))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="incident_agent.service")
    p.add_argument("--root", help="state directory (default $INCIDENT_SERVICE_DIR or .cache/service)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--mode", choices=("shadow", "assist"), default="shadow")
    s.add_argument(
        "--agent",
        default="llm_react",
        help="llm_react (default), llm_orchestrator, llm_swarm, react_baseline (no model)",
    )
    s.add_argument("--cascade", action="store_true")
    s.add_argument("--allow-remote", action="store_true")
    t = sub.add_parser("token")
    t.add_argument("action", choices=("add", "list", "remove"))
    t.add_argument("name", nargs="?", default="")
    t.add_argument("--scopes", default="read", help=f"comma list of {SCOPES}")
    r = sub.add_parser("roster")
    r.add_argument("action", choices=("add", "list"))
    r.add_argument("name", nargs="?", default="")
    r.add_argument("--roles", default="oncall")
    a = p.parse_args(argv)
    try:
        return {"serve": cmd_serve, "token": cmd_token, "roster": cmd_roster}[a.cmd](a)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
