"""Standalone MCP servers assembled from the project's existing tool specs.

The legacy :mod:`server` module remains the all-tools compatibility server.
These modules provide smaller capability boundaries so an agent can connect
only to the tools a deployment actually needs.
"""

from mcp_servers.common import build_server, run_server, serve_stdio

__all__ = ["build_server", "run_server", "serve_stdio"]
