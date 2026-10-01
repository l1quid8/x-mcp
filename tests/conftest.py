"""Use a reserved test origin; tests must never target a deployed MCP server."""

import os

os.environ["X_MCP_ORIGIN"] = "https://mcp.example.test"
