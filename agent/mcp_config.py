"""Configuration for stdio MCP servers used by the agent.

The config format deliberately contains process-launch details only.  Agent
policy and capability-specific settings belong to their respective MCP
servers, not to the generic host.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER_SCRIPT = REPO_ROOT / "server.py"
CURRENT_PYTHON_COMMAND = "{python}"
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SERVER_KEYS = {
    "name",
    "command",
    "args",
    "env",
    "inherit_env",
    "tool_prefix",
    "required",
}


class MCPConfigError(ValueError):
    """An MCP server config is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """How to launch one stdio MCP server.

    ``inherit_env`` is intentionally explicit.  Apart from the small runtime
    allowlist applied by :class:`agent.mcp_client.MCPToolClient`, ambient
    process variables are not sent to child processes.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    inherit_env: tuple[str, ...] = ()
    tool_prefix: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        _validate_name(self.name, "name")
        if not isinstance(self.command, str) or not self.command.strip():
            raise MCPConfigError("command must be a non-empty string")
        command = self.command.strip()
        if command == CURRENT_PYTHON_COMMAND:
            command = sys.executable

        args = _string_tuple(self.args, "args")
        inherited = _string_tuple(self.inherit_env, "inherit_env", non_empty=True)
        if len(set(inherited)) != len(inherited):
            raise MCPConfigError("inherit_env must not contain duplicate names")

        if not isinstance(self.env, Mapping):
            raise MCPConfigError("env must be an object of string values")
        explicit_env: dict[str, str] = {}
        for key, value in self.env.items():
            if not isinstance(key, str) or not key:
                raise MCPConfigError("env keys must be non-empty strings")
            if not isinstance(value, str):
                raise MCPConfigError(f"env[{key!r}] must be a string")
            explicit_env[key] = value

        if self.tool_prefix is not None:
            _validate_name(self.tool_prefix, "tool_prefix")
        if type(self.required) is not bool:
            raise MCPConfigError("required must be a boolean")

        # Normalise caller-provided lists and arbitrary mappings so a frozen
        # config has stable, copy-independent values.
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "args", args)
        object.__setattr__(self, "env", explicit_env)
        object.__setattr__(self, "inherit_env", inherited)

    def public_tool_name(self, tool_name: str) -> str:
        """Return the gateway-facing name for a server tool."""
        if self.tool_prefix is None:
            return tool_name
        return f"{self.tool_prefix}__{tool_name}"

    @classmethod
    def default_root(cls) -> "MCPServerConfig":
        """The repository's original translation/tool server.

        The old client inherited the whole parent environment.  Only the two
        settings read by current root-server tools are retained explicitly;
        gateway credentials are never part of this compatibility default.
        """
        return cls(
            name="root",
            command=sys.executable,
            args=(str(DEFAULT_SERVER_SCRIPT),),
            inherit_env=("GLOSSARY_CSV", "WEATHER_PROVIDER"),
        )


def default_mcp_server_configs() -> list[MCPServerConfig]:
    """Built-in capability servers used when no config file is supplied.

    Translation and general utilities deliberately run in different processes.
    The side-effecting RAG upload server is never auto-enabled; deployments add
    it explicitly through an MCP config after defining its filesystem boundary.
    """
    return [
        MCPServerConfig(
            name="utilities",
            command=sys.executable,
            args=("-m", "mcp_servers.utilities"),
            inherit_env=("WEATHER_PROVIDER",),
        ),
        MCPServerConfig(
            name="translation",
            command=sys.executable,
            args=("-m", "mcp_servers.translation"),
            inherit_env=("GLOSSARY_CSV",),
        ),
    ]


def load_mcp_server_configs(path: str | Path) -> list[MCPServerConfig]:
    """Load and validate a JSON list of stdio MCP server definitions.

    Both ``{"servers": [...]}`` and a bare ``[...]`` are accepted.  The
    object form is preferred because it can grow without changing the server
    entries themselves.
    """
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MCPConfigError(f"cannot read MCP config {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MCPConfigError(
            f"invalid JSON in MCP config {target} at line {exc.lineno}, column {exc.colno}"
        ) from exc

    if isinstance(raw, dict):
        unknown = set(raw) - {"servers"}
        if unknown:
            raise MCPConfigError(f"unknown top-level MCP config field(s): {_join(unknown)}")
        raw_servers = raw.get("servers")
    else:
        raw_servers = raw

    if not isinstance(raw_servers, list):
        raise MCPConfigError("MCP config must be a list or an object containing a servers list")
    if not raw_servers:
        raise MCPConfigError("MCP config must define at least one server")

    configs = [_server_from_json(item, index) for index, item in enumerate(raw_servers)]
    duplicate_names = _duplicates(config.name for config in configs)
    if duplicate_names:
        raise MCPConfigError(f"duplicate MCP server name(s): {_join(duplicate_names)}")
    return configs


# Concise alias for callers that already know they are loading MCP config.
load_mcp_config = load_mcp_server_configs


def _server_from_json(value: Any, index: int) -> MCPServerConfig:
    location = f"servers[{index}]"
    if not isinstance(value, dict):
        raise MCPConfigError(f"{location} must be an object")
    unknown = set(value) - _SERVER_KEYS
    if unknown:
        raise MCPConfigError(f"{location} has unknown field(s): {_join(unknown)}")

    missing = {"name", "command"} - set(value)
    if missing:
        raise MCPConfigError(f"{location} is missing required field(s): {_join(missing)}")

    try:
        return MCPServerConfig(
            name=value["name"],
            command=value["command"],
            args=value.get("args", ()),
            env=value.get("env", {}),
            inherit_env=value.get("inherit_env", ()),
            tool_prefix=value.get("tool_prefix"),
            required=value.get("required", True),
        )
    except MCPConfigError as exc:
        raise MCPConfigError(f"{location}: {exc}") from exc


def _validate_name(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value):
        raise MCPConfigError(
            f"{field_name} must match {_NAME_PATTERN.pattern!r} (letters, digits, '_' and '-')"
        )


def _string_tuple(
    value: Sequence[str] | Any,
    field_name: str,
    *,
    non_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MCPConfigError(f"{field_name} must be a list of strings")
    result = tuple(value)
    for item in result:
        if not isinstance(item, str) or (non_empty and not item):
            qualifier = "non-empty " if non_empty else ""
            raise MCPConfigError(f"{field_name} must contain only {qualifier}strings")
    return result


def _duplicates(values: Sequence[str] | Any) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _join(values: Sequence[str] | set[str]) -> str:
    return ", ".join(sorted(values))
