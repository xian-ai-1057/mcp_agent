"""MCP-backed tool runners for the generic agent loop.

``MCPToolClient`` preserves the original one-stdio-server API.  ``MCPToolPool``
adds a configurable multi-server host while presenting the same ``ToolRunner``
contract to the loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.bridge import OPENAI_TOOL_NAME, mcp_tool_to_openai, mcp_tools_to_openai
from agent.mcp_config import MCPConfigError, MCPServerConfig, load_mcp_server_configs
from agent.tooling import ToolInvocationError, ToolRunner

__all__ = [
    "LocalToolRunner",
    "MCPToolClient",
    "MCPToolPool",
    "ToolInvocationError",
    "ToolRunner",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "server.py"

DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0
MAX_TOOL_LIST_PAGES = 100

# Enough for a child process and Python runtime to start on supported systems,
# without copying application secrets into every MCP server.  Capability
# settings must be listed in ``inherit_env`` or supplied in ``env``.
RUNTIME_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
DEFAULT_ROOT_INHERIT_ENV = ("GLOSSARY_CSV", "WEATHER_PROVIDER")


class MCPToolClient:
    """``ToolRunner`` backed by one stdio MCP server.

    The first four constructor arguments are unchanged from the original API.
    New process-isolation and timeout controls are keyword-only.
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | tuple[str, ...] | None = None,
        env: dict[str, str] | None = None,
        errlog: Any | None = None,
        *,
        inherit_env: Iterable[str] | None = None,
        read_timeout_seconds: float | None = DEFAULT_READ_TIMEOUT_SECONDS,
        call_timeout_seconds: float | None = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self.command = command or sys.executable
        self.args = list(args) if args is not None else [str(SERVER_SCRIPT)]
        self.env = dict(env or {})
        # Direct construction historically meant the repository root server;
        # retain only the ambient variables its tools actually read.
        self.inherit_env = tuple(
            DEFAULT_ROOT_INHERIT_ENV if inherit_env is None else inherit_env
        )
        self.read_timeout_seconds = _timeout_value(read_timeout_seconds, "read timeout")
        self.call_timeout_seconds = _timeout_value(call_timeout_seconds, "call timeout")
        # The server logs to stderr by design (stdout is the JSON-RPC channel).
        self.errlog = errlog
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "MCPToolClient":
        if self._stack is not None:
            raise RuntimeError("MCPToolClient is already connected")

        stack = AsyncExitStack()
        self._stack = stack
        try:
            params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=_child_environment(self.env, self.inherit_env),
            )
            client = (
                stdio_client(params)
                if self.errlog is None
                else stdio_client(params, self.errlog)
            )
            read, write = await stack.enter_async_context(client)
            session_kwargs: dict[str, Any] = {}
            if self.read_timeout_seconds is not None:
                session_kwargs["read_timeout_seconds"] = timedelta(
                    seconds=self.read_timeout_seconds
                )
            self._session = await stack.enter_async_context(
                ClientSession(read, write, **session_kwargs)
            )
            await self._session.initialize()
            self._tools = await self._list_all_tools()
            return self
        except asyncio.CancelledError:
            await self._close_failed_enter(stack)
            raise
        except Exception as exc:
            await self._close_failed_enter(stack)
            if isinstance(exc, ToolInvocationError):
                raise
            raise ToolInvocationError(
                f"failed to connect to MCP server ({self.command!r}): {exc}"
            ) from exc

    async def _list_all_tools(self) -> list[Any]:
        """Collect every MCP tools/list page and reject broken cursor loops."""
        if self._session is None:
            raise RuntimeError("MCP session is not connected")

        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_TOOL_LIST_PAGES):
            page = (
                await self._session.list_tools()
                if cursor is None
                else await self._session.list_tools(cursor=cursor)
            )
            page_tools = getattr(page, "tools", None)
            if not isinstance(page_tools, list):
                raise ToolInvocationError("MCP tools/list response has no tools list")
            tools.extend(page_tools)

            next_cursor = getattr(page, "nextCursor", None)
            if next_cursor is None:
                return tools
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ToolInvocationError("MCP tools/list returned an invalid cursor")
            if next_cursor in seen_cursors:
                raise ToolInvocationError("MCP tools/list cursor cycle detected")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise ToolInvocationError(
            f"MCP tools/list exceeded the {MAX_TOOL_LIST_PAGES}-page safety limit"
        )

    async def _close_failed_enter(self, stack: AsyncExitStack) -> None:
        """Best-effort cleanup without allowing teardown to mask the cause."""
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._reset()

    async def __aexit__(self, *exc_info: Any) -> None:
        stack = self._stack
        self._reset()
        if stack is not None:
            await stack.aclose()

    def _reset(self) -> None:
        self._stack = None
        self._session = None
        self._tools = []

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

        call_kwargs: dict[str, Any] = {}
        if self.call_timeout_seconds is not None:
            call_kwargs["read_timeout_seconds"] = timedelta(
                seconds=self.call_timeout_seconds
            )
        try:
            result = await self._session.call_tool(name, arguments, **call_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ToolInvocationError(f"MCP tool {name!r} transport failed: {exc}") from exc

        text = _result_text(result)
        if getattr(result, "isError", False):
            raise ToolInvocationError(text or f"tool {name!r} failed")
        return text


@dataclass(frozen=True, slots=True)
class _ToolBinding:
    server_name: str
    remote_name: str
    client: MCPToolClient


class MCPToolPool:
    """Aggregate multiple stdio MCP servers behind one ``ToolRunner``.

    Public tool names stay unchanged unless a server declares ``tool_prefix``;
    prefixed tools use ``prefix__remote_name`` so the names remain compatible
    with OpenAI-style function schemas.  Any remaining collision is a startup
    error, never an order-dependent dispatch decision.
    """

    def __init__(
        self,
        configs: Sequence[MCPServerConfig] | None = None,
        *,
        errlog: Any | None = None,
        read_timeout_seconds: float | None = DEFAULT_READ_TIMEOUT_SECONDS,
        call_timeout_seconds: float | None = DEFAULT_CALL_TIMEOUT_SECONDS,
        client_factory: Callable[..., MCPToolClient] | None = None,
    ) -> None:
        self.configs = list(configs) if configs is not None else [MCPServerConfig.default_root()]
        if not self.configs:
            raise MCPConfigError("MCPToolPool requires at least one server")
        duplicates = _duplicates(config.name for config in self.configs)
        if duplicates:
            names = ", ".join(sorted(duplicates))
            raise MCPConfigError(f"duplicate MCP server name(s): {names}")

        self.errlog = errlog
        self.read_timeout_seconds = _timeout_value(read_timeout_seconds, "read timeout")
        self.call_timeout_seconds = _timeout_value(call_timeout_seconds, "call timeout")
        self._client_factory = client_factory or MCPToolClient
        self._stack: AsyncExitStack | None = None
        self._clients: dict[str, MCPToolClient] = {}
        self._bindings: dict[str, _ToolBinding] = {}
        self._openai_tools: list[dict[str, Any]] = []
        self._connection_errors: dict[str, str] = {}

    @classmethod
    def from_json(cls, path: str | Path, **kwargs: Any) -> "MCPToolPool":
        return cls(load_mcp_server_configs(path), **kwargs)

    async def __aenter__(self) -> "MCPToolPool":
        if self._stack is not None:
            raise RuntimeError("MCPToolPool is already connected")

        stack = AsyncExitStack()
        self._stack = stack
        try:
            for config in self.configs:
                client = self._client_factory(
                    command=config.command,
                    args=list(config.args),
                    env=dict(config.env),
                    errlog=self.errlog,
                    inherit_env=config.inherit_env,
                    read_timeout_seconds=self.read_timeout_seconds,
                    call_timeout_seconds=self.call_timeout_seconds,
                )
                try:
                    connected = await stack.enter_async_context(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if config.required:
                        raise ToolInvocationError(
                            f"required MCP server {config.name!r} failed to start: {exc}"
                        ) from exc
                    self._connection_errors[config.name] = str(exc)
                    continue

                self._clients[config.name] = connected
                self._register_tools(config, connected)
            return self
        except asyncio.CancelledError:
            await self._close_failed_enter(stack)
            raise
        except Exception:
            await self._close_failed_enter(stack)
            raise

    def _register_tools(self, config: MCPServerConfig, client: MCPToolClient) -> None:
        for tool in client.mcp_tools:
            public_name = config.public_tool_name(tool.name)
            if not OPENAI_TOOL_NAME.fullmatch(public_name):
                raise MCPConfigError(
                    f"public tool name {public_name!r} is not gateway-compatible; "
                    "shorten tool_prefix or the remote tool name"
                )
            prior = self._bindings.get(public_name)
            if prior is not None:
                raise MCPConfigError(
                    f"tool name collision {public_name!r}: "
                    f"{prior.server_name!r}/{prior.remote_name!r} and "
                    f"{config.name!r}/{tool.name!r}; configure tool_prefix"
                )

            self._bindings[public_name] = _ToolBinding(
                server_name=config.name,
                remote_name=tool.name,
                client=client,
            )
            definition = deepcopy(mcp_tool_to_openai(tool))
            definition["function"]["name"] = public_name
            self._openai_tools.append(definition)

    async def _close_failed_enter(self, stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._reset()

    async def __aexit__(self, *exc_info: Any) -> None:
        stack = self._stack
        self._reset()
        if stack is not None:
            await stack.aclose()

    def _reset(self) -> None:
        self._stack = None
        self._clients = {}
        self._bindings = {}
        self._openai_tools = []
        self._connection_errors = {}

    @property
    def openai_tools(self) -> list[dict[str, Any]]:
        return deepcopy(self._openai_tools)

    @property
    def tool_names(self) -> set[str]:
        return set(self._bindings)

    @property
    def connection_errors(self) -> dict[str, str]:
        """Startup errors for optional servers that were skipped."""
        return dict(self._connection_errors)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._stack is None:
            raise RuntimeError("MCPToolPool must be used as an async context manager")
        binding = self._bindings.get(name)
        if binding is None:
            raise ToolInvocationError(f"unknown tool {name!r}")
        try:
            return await binding.client.call(binding.remote_name, arguments)
        except asyncio.CancelledError:
            raise
        except ToolInvocationError:
            raise
        except Exception as exc:
            raise ToolInvocationError(
                f"MCP tool {name!r} on server {binding.server_name!r} failed: {exc}"
            ) from exc


def _child_environment(explicit: dict[str, str], inherited: Iterable[str]) -> dict[str, str]:
    """Build a least-privilege child environment.

    ``GATEWAY_API_KEY`` is deliberately absent from the runtime allowlist.  It
    can only cross the process boundary when a server explicitly names it in
    ``inherit_env`` or provides it in ``env``.
    """
    child_env = {
        name: os.environ[name]
        for name in RUNTIME_ENV_ALLOWLIST
        if name in os.environ
    }
    for name in inherited:
        if name in os.environ:
            child_env[name] = os.environ[name]
    child_env.update(explicit)

    # Preserve the original root-server development behaviour: it can import
    # this repository even before the package is installed.
    python_path = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), python_path) if part
    )
    return child_env


def _result_text(result: Any) -> str:
    """Serialise structured/text results and reject unsupported typed blocks."""
    missing = object()
    structured = getattr(result, "structuredContent", missing)
    if structured is missing:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            return json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ToolInvocationError(
                f"MCP structured result is not JSON serialisable: {exc}"
            ) from exc

    parts: list[str] = []
    unsupported: list[str] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(str(getattr(block, "text", "")))
        else:
            unsupported.append(str(block_type or type(block).__name__))
    if unsupported:
        kinds = ", ".join(sorted(set(unsupported)))
        raise ToolInvocationError(f"unsupported non-text MCP result block(s): {kinds}")
    return "\n".join(parts)


def _timeout_value(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number or None") from exc
    if timeout <= 0:
        raise ValueError(f"{label} must be a positive number or None")
    return timeout


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


class LocalToolRunner:
    """``ToolRunner`` that calls tool handlers in-process, bypassing MCP.

    Production goes through an MCP-backed runner.  This implementation remains
    useful for loop tests and the eval harness, where subprocess startup would
    dominate runtime.
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
