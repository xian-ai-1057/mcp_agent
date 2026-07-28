"""Helper for tests that need a live MCP server.

Deliberately *not* a pytest fixture. pytest-asyncio finalises async fixtures in a
different task from the one that created them, and anyio's cancel scopes — which
`stdio_client` uses — refuse to be exited from another task. Entering and leaving
the session inside the test body keeps both ends on the same task.
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from agent.mcp_client import MCPToolClient


@asynccontextmanager
async def mcp_session(env: dict[str, str] | None = None) -> AsyncIterator[MCPToolClient]:
    """Spawn `server.py`, yield a connected client, tear it down."""
    with open(os.devnull, "w") as devnull:
        async with MCPToolClient(env=env, errlog=devnull) as client:
            yield client
