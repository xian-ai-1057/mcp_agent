"""MCP tool schema ↔ OpenAI `tools` format.

Mechanical and total. This is the single place where "the gateway wants a
slightly different shape" gets absorbed, which is what keeps that risk out of
the loop.

See `specs/003-agent-client/spec.md` §4.
"""

import json
from typing import Any

from agent.gateway import AssistantTurn, ToolCall


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    """One `mcp.types.Tool` (or any object with the same attributes) → OpenAI."""
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
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in turn.tool_calls
        ]
    return message


def tool_result_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
    }


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}
