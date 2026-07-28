"""Unit tests for `agent.bridge` (spec 003 §4)."""

import json
from types import SimpleNamespace

import mcp.types as types
import pytest

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

    @pytest.mark.parametrize("name", ["a" * 65, "bad tool", ""])
    def test_gateway_incompatible_tool_names_fail_at_the_bridge(self, name):
        tool = SimpleNamespace(name=name, inputSchema={"type": "object"})
        with pytest.raises(ValueError, match="not compatible"):
            mcp_tool_to_openai(tool)

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

    def test_malformed_arguments_are_replayed_verbatim(self):
        turn = AssistantTurn(
            tool_calls=(
                ToolCall(
                    id="c",
                    name="get_time",
                    raw_arguments="{not json",
                    parse_error="bad JSON",
                ),
            )
        )
        assert assistant_message(turn)["tool_calls"][0]["function"]["arguments"] == "{not json"

    def test_legacy_function_call_round_trip_is_symmetric(self):
        call = ToolCall(
            id="legacy",
            name="get_time",
            arguments={},
            raw_arguments="{}",
            protocol="function_call",
        )
        assert assistant_message(AssistantTurn(tool_calls=(call,))) == {
            "role": "assistant",
            "content": "",
            "function_call": {"name": "get_time", "arguments": "{}"},
        }
        assert tool_result_message(call, "result") == {
            "role": "function",
            "name": "get_time",
            "content": "result",
        }

    def test_none_content_becomes_empty_string(self):
        assert assistant_message(AssistantTurn(content=None))["content"] == ""

    def test_tool_result_message(self):
        call = ToolCall(id="c1", name="get_time")
        assert tool_result_message(call, '{"time": "12:00"}') == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": '{"time": "12:00"}',
        }

    def test_tool_result_uses_only_fields_allowed_by_modern_chat_completions(self):
        message = tool_result_message(ToolCall(id="c1", name="get_time"), "ok")
        assert set(message) == {"role", "tool_call_id", "content"}

    def test_user_and_system_messages(self):
        assert user_message("hi") == {"role": "user", "content": "hi"}
        assert system_message("rules") == {"role": "system", "content": "rules"}
