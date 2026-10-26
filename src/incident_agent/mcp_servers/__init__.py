"""Stage 13: the incident data sources as MCP servers (logs, metrics, deploys, sandbox diagnostics).

Layout: ``backend`` holds the tool logic (SDK-independent, incident-scoped, read-only), ``diagnostics`` the
allow-listed analysis scripts that run in a separate process, ``servers`` the thin MCP adapters (one server per
data source) and ``client`` an ``IncidentToolbox``-compatible facade that talks to those servers over MCP.
"""
