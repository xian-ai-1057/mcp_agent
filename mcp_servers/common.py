"""Shared low-level MCP server assembly and stdio entry helpers."""

import asyncio
import logging
import sys
from collections.abc import Mapping
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from tools.base import ToolError, ToolSpec


def build_server(
    specs: Mapping[str, ToolSpec],
    server_name: str,
    version: str,
) -> Server:
    """Build a low-level MCP server backed by an explicit tool-spec registry.

    ``version`` is accepted here as part of the complete server identity even
    though the SDK uses it during ``Server.run`` rather than construction. This
    keeps every caller explicit about the identity it will advertise.
    """
    if not server_name.strip():
        raise ValueError("server_name must not be empty")
    if not version.strip():
        raise ValueError("version must not be empty")

    registry = dict(specs)
    for name, spec in registry.items():
        if not isinstance(spec, ToolSpec):
            raise TypeError(f"spec {name!r} must be a ToolSpec, got {type(spec).__name__}")
        if name != spec.name:
            raise ValueError(f"registry key {name!r} does not match tool name {spec.name!r}")

    server = Server(server_name)

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
            available = ", ".join(sorted(registry))
            raise ToolError(f"unknown tool {name!r}; available: {available}")
        return await asyncio.to_thread(spec.run, arguments)

    return server


async def serve_stdio(
    specs: Mapping[str, ToolSpec],
    server_name: str,
    version: str,
) -> None:
    """Serve an explicit registry over stdio until the client disconnects."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = build_server(specs, server_name, version)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=server_name,
                server_version=version,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def run_server(
    specs: Mapping[str, ToolSpec],
    server_name: str,
    version: str,
) -> None:
    """Synchronous console-script wrapper around :func:`serve_stdio`."""
    asyncio.run(serve_stdio(specs, server_name, version))
