"""Run one incident MCP server over stdio: ``python -m incident_agent.mcp_servers logs --incident INC-001``."""

from __future__ import annotations

import argparse

from incident_agent.mcp_servers.servers import SERVER_KINDS, build_server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve one incident data source over MCP (stdio)")
    ap.add_argument("kind", choices=SERVER_KINDS)
    ap.add_argument("--incident", required=True, help="incident id, e.g. INC-001")
    args = ap.parse_args(argv)
    try:
        server = build_server(args.kind, args.incident)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
