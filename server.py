"""MCP stdio server.

Registers whatever `tools/` contains and translates between MCP and the tool
contract. It holds no business logic and names no tool. Adding a tool must not
require touching this file — acceptance criterion 6 checks its hash.

Run directly (`python server.py`) or let `agent/mcp_client.py` spawn it.

See `specs/002-mcp-tools/spec.md` §6.
"""

import asyncio
import logging
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from tools.base import ToolError, ToolSpec
from tools.registry import discover

SERVER_NAME = "mcp-agent-tools"
SERVER_VERSION = "0.3.0"

logger = logging.getLogger(SERVER_NAME)


def _configure_logging() -> None:
    """stderr only. stdout is the JSON-RPC channel; a stray write corrupts it."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_server(specs: dict[str, ToolSpec] | None = None) -> Server:
    registry = discover() if specs is None else specs
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for spec in registry.values()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = registry.get(name)
        if spec is None:
            raise ToolError(f"unknown tool {name!r}; available: {', '.join(sorted(registry))}")
        # Arguments are already validated against input_schema by the SDK.
        # Handlers are synchronous by contract, so keep the event loop free.
        return await asyncio.to_thread(spec.run, arguments)

    return server


async def main() -> None:
    _configure_logging()
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
