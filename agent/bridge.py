"""MCP tool schema ↔ OpenAI `tools` format.

Mechanical and total. This is the single place where "the gateway wants a
slightly different shape" gets absorbed, which is what keeps that risk out of
the loop.

See `specs/003-agent-client/spec.md` §4.
"""

import json
import re
from typing import Any

from agent.gateway import AssistantTurn, ToolCall

OPENAI_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    """One `mcp.types.Tool` (or any object with the same attributes) → OpenAI."""
    if not isinstance(tool.name, str) or not OPENAI_TOOL_NAME.fullmatch(tool.name):
        raise ValueError(
            f"MCP tool name {tool.name!r} is not compatible with the gateway "
            "(expected 1-64 letters, digits, underscores, or hyphens)"
        )
    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "parameters": schema,
        },
    }


def mcp_tools_to_openai(tools: list[Any]) -> list[dict[str, Any]]:
    return [mcp_tool_to_openai(tool) for tool in tools]


def assistant_message(turn: AssistantTurn) -> dict[str, Any]:
    """Render an assistant turn back into the message list.

    `arguments` is re-emitted as a JSON *string* regardless of how the gateway
    sent it, because that is what the wire format specifies and what gateways
    reliably accept on the way back in.
    """
    message: dict[str, Any] = {"role": "assistant", "content": turn.content or ""}
    if turn.tool_calls:
        if all(call.protocol == "function_call" for call in turn.tool_calls):
            if len(turn.tool_calls) != 1:
                raise ValueError("legacy function_call protocol supports exactly one call")
            call = turn.tool_calls[0]
            message["function_call"] = {
                "name": call.name,
                "arguments": (
                    call.raw_arguments
                    if call.parse_error
                    else json.dumps(call.arguments, ensure_ascii=False)
                ),
            }
            return message
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": (
                        call.raw_arguments
                        if call.parse_error
                        else json.dumps(call.arguments, ensure_ascii=False)
                    ),
                },
            }
            for call in turn.tool_calls
        ]
    return message


def tool_result_message(call: ToolCall, content: str) -> dict[str, Any]:
    """Render a modern Chat Completions tool result message.

    ``name`` is deliberately omitted.  It belongs to the deprecated
    ``role=function`` message shape, not to a modern ``role=tool`` message; a
    number of strict OpenAI-compatible gateways reject the extra field.
    ``tool_call_id`` is the canonical link back to the assistant tool call.
    """
    if call.protocol == "function_call":
        return {"role": "function", "name": call.name, "content": content}
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": content,
    }


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}
