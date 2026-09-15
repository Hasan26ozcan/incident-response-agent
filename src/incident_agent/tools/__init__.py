"""Tool integrations — MCP servers and MCP client wrappers.

Populated starting Stage 13 (MCP servers for log/metric/deploy/sandbox
access) through Stage 16 (sandboxed execution safety). Every tool an agent
can call is expected to be reachable only through this package, per the
Stage 0 architecture decision to make MCP the sole tool-access layer — see
openspec/proposal.md §4.
"""
