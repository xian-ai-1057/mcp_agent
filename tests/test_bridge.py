"""Unit tests for `agent.bridge` (spec 003 §4)."""

import json

import mcp.types as types

from agent.bridge import (
    assistant_message,
    mcp_tool_to_openai,
    mcp_tools_to_openai,
    system_message,
    tool_result_message,
    user_message,
)
from agent.gateway import AssistantTurn, ToolCall
from tools.registry import discover


class TestMcpToOpenAI:
    def test_shape(self):
        tool = types.Tool(
            name="get_time",
            description="reads the clock",
            inputSchema={"type": "object", "properties": {}},
        )
        assert mcp_tool_to_openai(tool) == {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "reads the clock",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def test_missing_description_becomes_empty_string(self):
        tool = types.Tool(name="t", inputSchema={"type": "object"})
        assert mcp_tool_to_openai(tool)["function"]["description"] == ""

    def test_round_trips_every_real_tool(self):
        """Converted against the actual registry, not a hand-written stub."""
        specs = discover()
        tools = [
            types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in specs.values()
        ]
        converted = mcp_tools_to_openai(tools)

        assert len(converted) == len(specs)
        for entry in converted:
            function = entry["function"]
            assert entry["type"] == "function"
            assert function["name"] in specs
            assert function["description"] == specs[function["name"]].description
            assert function["parameters"] == specs[function["name"]].input_schema
            json.dumps(entry)  # must survive the wire


class TestMessageBuilders:
    def test_assistant_message_without_tool_calls(self):
        assert assistant_message(AssistantTurn(content="hi")) == {
            "role": "assistant",
            "content": "hi",
        }

    def test_assistant_message_re_encodes_arguments_as_a_json_string(self):
        """Whatever the gateway sent, send back what the wire format specifies."""
        turn = AssistantTurn(
            tool_calls=(ToolCall(id="c1", name="get_time", arguments={"timezone": "Asia/Taipei"}),)
        )
        message = assistant_message(turn)
        arguments = message["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"timezone": "Asia/Taipei"}

    def test_assistant_message_keeps_non_ascii_readable(self):
        turn = AssistantTurn(tool_calls=(ToolCall(id="c", name="t", arguments={"text": "額度"}),))
        assert "額度" in assistant_message(turn)["tool_calls"][0]["function"]["arguments"]

    def test_none_content_becomes_empty_string(self):
        assert assistant_message(AssistantTurn(content=None))["content"] == ""

    def test_tool_result_message(self):
        call = ToolCall(id="c1", name="get_time")
        assert tool_result_message(call, '{"time": "12:00"}') == {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "get_time",
            "content": '{"time": "12:00"}',
        }

    def test_user_and_system_messages(self):
        assert user_message("hi") == {"role": "user", "content": "hi"}
        assert system_message("rules") == {"role": "system", "content": "rules"}
