"""End-to-end tests against the real stdio MCP server (spec 002 §6).

These spawn `server.py` as a subprocess and speak MCP to it, so they cover the
transport, the schema validation the SDK performs, and the error mapping — the
parts an in-process test cannot reach.

Assertions are grouped a few to a session because each session is a subprocess
spawn; see `tests/mcp_session.py` for why the session is not a fixture.
"""

import json

import pytest

from agent.mcp_client import ToolInvocationError
from tests.mcp_session import mcp_session
from tools.registry import discover


class TestListTools:
    async def test_advertises_every_registered_tool_faithfully(self):
        specs = discover()
        async with mcp_session() as server:
            assert server.tool_names == set(specs)

            for tool in server.mcp_tools:
                assert tool.description == specs[tool.name].description
                assert tool.inputSchema == specs[tool.name].input_schema

            converted = {t["function"]["name"] for t in server.openai_tools}
            assert converted == server.tool_names


class TestCallTool:
    async def test_round_trips(self):
        async with mcp_session() as server:
            lookup = json.loads(await server.call("lookup_terms", {"text": "客戶申請提高臨時額度"}))
            assert lookup["count"] == 1
            assert lookup["matches"][0]["zh"] == "臨時額度"

            clock = json.loads(await server.call("get_time", {"timezone": "Asia/Taipei"}))
            assert clock["timezone"] == "Asia/Taipei"

            verified = json.loads(
                await server.call(
                    "verify_translation",
                    {"source_text": "臨時額度", "translation": "the temporary credit limit"},
                )
            )
            assert verified["hit_rate"] == 1.0

            greeting = json.loads(await server.call("say_hello", {"name": "Alex", "language": "en"}))
            assert greeting["greeting"].startswith("Hello, Alex")


class TestErrorMapping:
    async def test_failures_are_error_results_and_the_session_survives(self):
        async with mcp_session() as server:
            with pytest.raises(ToolInvocationError, match="unknown timezone"):
                await server.call("get_time", {"timezone": "Mars/Olympus"})

            with pytest.raises(ToolInvocationError):
                await server.call("no_such_tool", {})

            # Schema violations are caught by the SDK before the handler runs.
            with pytest.raises(ToolInvocationError):
                await server.call("say_hello", {"name": "Alex", "language": "klingon"})

            with pytest.raises(ToolInvocationError):
                await server.call("lookup_terms", {})

            with pytest.raises(ToolInvocationError):
                await server.call("say_hello", {"name": "Alex", "extra": "not allowed"})

            # One broken tool must never take the session down.
            payload = json.loads(await server.call("get_time", {}))
            assert payload["timezone"] == "Asia/Taipei"
