"""Standalone MCP server for glossary-backed translation tools."""

from mcp_servers.common import run_server
from tools.base import ToolSpec
from tools.translate_lookup import SPEC as LOOKUP_TERMS
from tools.translate_verify import SPEC as VERIFY_TRANSLATION

SERVER_NAME = "mcp-agent-translation"
SERVER_VERSION = "0.4.0"
TOOL_SPECS = (LOOKUP_TERMS, VERIFY_TRANSLATION)
TOOL_NAMES = tuple(spec.name for spec in TOOL_SPECS)


def discover_specs() -> dict[str, ToolSpec]:
    """Return only the translation capability's tool specs."""
    return {spec.name: spec for spec in TOOL_SPECS}


def main() -> None:
    """Run the translation server on stdio."""
    run_server(discover_specs(), SERVER_NAME, SERVER_VERSION)


if __name__ == "__main__":
    main()
