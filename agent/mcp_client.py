"""Spawn `server.py` over stdio and expose its tools to the loop.

The loop talks to `ToolRunner`; this is the MCP-backed implementation. Nothing
above this module handles a JSON-RPC frame, and nothing below it knows a model
exists.

See `specs/003-agent-client/spec.md` §1.
"""

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.bridge import mcp_tools_to_openai

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "server.py"


class ToolInvocationError(Exception):
    """The tool ran and reported a failure. Recoverable; shown to the model."""


class ToolRunner(Protocol):
    """What the agent loop needs in order to use tools."""

    @property
    def openai_tools(self) -> list[dict[str, Any]]: ...

    @property
    def tool_names(self) -> set[str]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class MCPToolClient:
    """`ToolRunner` backed by the stdio MCP server in this repository."""

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        errlog: Any | None = None,
    ) -> None:
        self.command = command or sys.executable
        self.args = args if args is not None else [str(SERVER_SCRIPT)]
        self.env = env
        # The server logs to stderr by design (stdout is the JSON-RPC channel).
        # Callers that want a clean console redirect it; None keeps the default.
        self.errlog = errlog
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "MCPToolClient":
        self._stack = AsyncExitStack()
        # The server imports this repository's packages, so the child needs the
        # repo on PYTHONPATH even when it is not installed.
        child_env = {**os.environ, **(self.env or {})}
        child_env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(REPO_ROOT), child_env.get("PYTHONPATH", "")])
        )
        params = StdioServerParameters(command=self.command, args=self.args, env=child_env)
        client = stdio_client(params) if self.errlog is None else stdio_client(params, self.errlog)
        read, write = await self._stack.enter_async_context(client)
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._tools = (await self._session.list_tools()).tools
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    @property
    def mcp_tools(self) -> list[Any]:
        return list(self._tools)

    @property
    def openai_tools(self) -> list[dict[str, Any]]:
        return mcp_tools_to_openai(self._tools)

    @property
    def tool_names(self) -> set[str]:
        return {tool.name for tool in self._tools}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError("MCPToolClient must be used as an async context manager")

        result = await self._session.call_tool(name, arguments)
        text = _result_text(result)
        if getattr(result, "isError", False):
            raise ToolInvocationError(text or f"tool {name!r} failed")
        return text


def _result_text(result: Any) -> str:
    """Prefer structured content; fall back to concatenated text blocks."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return json.dumps(structured, ensure_ascii=False)
    parts = [
        block.text
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts)


class LocalToolRunner:
    """`ToolRunner` that calls tool handlers in-process, bypassing MCP.

    For tests that are about loop behaviour rather than transport, and for the
    eval harness where spawning a subprocess per run would dominate the runtime.
    Production always goes through `MCPToolClient`.
    """

    def __init__(self, specs: dict[str, Any]) -> None:
        self._specs = specs

    @property
    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
            for spec in self._specs.values()
        ]

    @property
    def tool_names(self) -> set[str]:
        return set(self._specs)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolInvocationError(f"unknown tool {name!r}")
        try:
            return json.dumps(spec.run(arguments), ensure_ascii=False)
        except Exception as exc:
            raise ToolInvocationError(str(exc)) from exc
