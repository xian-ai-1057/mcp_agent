"""Standalone MCP server for general-purpose utility tools."""

from mcp_servers.common import run_server
from tools.base import ToolSpec
from tools.get_time import SPEC as GET_TIME
from tools.get_weather import SPEC as GET_WEATHER
from tools.say_hello import SPEC as SAY_HELLO

SERVER_NAME = "mcp-agent-utilities"
SERVER_VERSION = "0.4.0"
TOOL_SPECS = (SAY_HELLO, GET_TIME, GET_WEATHER)
TOOL_NAMES = tuple(spec.name for spec in TOOL_SPECS)


def discover_specs() -> dict[str, ToolSpec]:
    """Return only utility specs, preserving the declared display order."""
    return {spec.name: spec for spec in TOOL_SPECS}


def main() -> None:
    """Run the utility server on stdio."""
    run_server(discover_specs(), SERVER_NAME, SERVER_VERSION)


if __name__ == "__main__":
    main()
