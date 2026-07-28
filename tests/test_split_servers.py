"""Contract tests for the capability-specific MCP servers."""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_servers import translation, utilities


async def _listed_tools(module_name: str) -> set[str]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", module_name],
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return {tool.name for tool in result.tools}


def test_capability_registries_partition_the_legacy_tool_set():
    utility_specs = utilities.discover_specs()
    translation_specs = translation.discover_specs()

    assert set(utility_specs) == {"say_hello", "get_time", "get_weather"}
    assert set(translation_specs) == {"lookup_terms", "verify_translation"}
    assert utility_specs.keys().isdisjoint(translation_specs)


async def test_utility_server_lists_only_utility_tools():
    assert await _listed_tools("mcp_servers.utilities") == {
        "say_hello",
        "get_time",
        "get_weather",
    }


async def test_translation_server_lists_only_translation_tools():
    listed = await _listed_tools("mcp_servers.translation")

    assert listed == {"lookup_terms", "verify_translation"}
    assert listed.isdisjoint({"say_hello", "get_time", "get_weather"})
