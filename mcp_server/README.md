# mcp_server

The tool server (`python -m mcp_server.server`, 127.0.0.1:8000): eleven tools on `/mcp/tracer/`, `/mcp/adjudicator/` and `/mcp/verifier/`, each path listing only its agent's tools (CONTRACT.md), plus the handoff API. `tools.py` holds the tools as plain functions, `network.py` their read-only queries (QUERIES.md).
