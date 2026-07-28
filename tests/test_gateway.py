"""Response-reading tests for `agent.gateway` (spec 003 §2).

The plan names gateway format drift as the top integration risk. Each test below
is a payload shape a real OpenAI-compatible endpoint has been observed to emit;
adding a newly discovered quirk should mean adding a case here, not editing the
agent loop.
"""

import json

import httpx
import pytest

from agent.gateway import GatewayError, HTTPGateway, parse_assistant_message
from agent.loop import AgentLoop
from agent.mcp_client import LocalToolRunner
from tools.get_time import SPEC as GET_TIME


def _tool_call(name="get_time", arguments='{"timezone": "Asia/Taipei"}', call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class TestParseAssistantMessage:
    def test_plain_text(self):
        turn = parse_assistant_message({"role": "assistant", "content": "hello"})
        assert turn.content == "hello"
        assert turn.tool_calls == ()
        assert not turn.wants_tools

    def test_tool_calls_absent_null_or_empty_all_mean_no_call(self):
        for message in (
            {"content": "hi"},
            {"content": "hi", "tool_calls": None},
            {"content": "hi", "tool_calls": []},
        ):
            assert parse_assistant_message(message).tool_calls == ()

    def test_arguments_as_json_string(self):
        turn = parse_assistant_message({"tool_calls": [_tool_call()]})
        assert turn.tool_calls[0].arguments == {"timezone": "Asia/Taipei"}
        assert turn.tool_calls[0].parse_error is None

    def test_arguments_already_decoded_as_an_object(self):
        """Several gateways skip the JSON-string encoding entirely."""
        turn = parse_assistant_message({"tool_calls": [_tool_call(arguments={"timezone": "UTC"})]})
        assert turn.tool_calls[0].arguments == {"timezone": "UTC"}
        assert turn.tool_calls[0].parse_error is None

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_arguments_mean_no_arguments(self, empty):
        turn = parse_assistant_message({"tool_calls": [_tool_call(arguments=empty)]})
        assert turn.tool_calls[0].arguments == {}
        assert turn.tool_calls[0].parse_error is None

    def test_malformed_arguments_are_recorded_not_raised(self):
        turn = parse_assistant_message({"tool_calls": [_tool_call(arguments="{not json")]})
        call = turn.tool_calls[0]
        assert call.arguments == {}
        assert "not valid JSON" in call.parse_error
        assert call.raw_arguments == "{not json"

    def test_arguments_decoding_to_a_non_object_are_recorded(self):
        turn = parse_assistant_message({"tool_calls": [_tool_call(arguments="[1, 2]")]})
        assert "must decode to an object" in turn.tool_calls[0].parse_error

    def test_legacy_single_function_call_field(self):
        turn = parse_assistant_message(
            {"function_call": {"name": "get_time", "arguments": "{}"}}
        )
        assert turn.tool_calls[0].name == "get_time"

    def test_missing_id_is_synthesised(self):
        turn = parse_assistant_message(
            {"tool_calls": [{"function": {"name": "get_time", "arguments": "{}"}}]}
        )
        assert turn.tool_calls[0].id

    def test_nameless_call_is_a_protocol_error(self):
        with pytest.raises(GatewayError, match="no function name"):
            parse_assistant_message({"tool_calls": [_tool_call(name="")]})

    @pytest.mark.parametrize(
        "message",
        [
            {"tool_calls": "not-a-list"},
            {"tool_calls": ["not-an-object"]},
            {"tool_calls": [{"function": "not-an-object"}]},
            "not-an-object",
        ],
    )
    def test_malformed_message_shapes_are_gateway_errors(self, message):
        with pytest.raises(GatewayError):
            parse_assistant_message(message)

    def test_content_returned_as_parts(self):
        turn = parse_assistant_message(
            {"content": [{"type": "text", "text": "one "}, {"type": "text", "text": "two"}]}
        )
        assert turn.content == "one two"

    def test_content_and_tool_calls_together(self):
        turn = parse_assistant_message({"content": "thinking", "tool_calls": [_tool_call()]})
        assert turn.content == "thinking"
        assert turn.wants_tools

    def test_refusal_is_preserved_from_top_level_or_content_parts(self):
        top_level = parse_assistant_message({"content": None, "refusal": "cannot comply"})
        assert top_level.refusal == "cannot comply"

        content_part = parse_assistant_message(
            {"content": [{"type": "refusal", "refusal": "blocked"}]}
        )
        assert content_part.refusal == "blocked"

    def test_multiple_calls_in_one_turn(self):
        turn = parse_assistant_message(
            {"tool_calls": [_tool_call(call_id="a"), _tool_call(name="say_hello", call_id="b")]}
        )
        assert [c.name for c in turn.tool_calls] == ["get_time", "say_hello"]


class TestHTTPGateway:
    def _gateway(self, handler):
        transport = httpx.MockTransport(handler)
        return HTTPGateway(
            base_url="https://gateway.example/v1",
            api_key="secret",
            model="fedgpt-medium",
            client=httpx.AsyncClient(transport=transport),
        )

    async def test_posts_to_chat_completions_with_tools(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        gateway = self._gateway(handler)
        tools = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]
        turn = await gateway.complete([{"role": "user", "content": "hi"}], tools=tools)

        assert turn.content == "ok"
        assert seen["url"] == "https://gateway.example/v1/chat/completions"
        assert seen["auth"] == "Bearer secret"
        assert seen["body"]["model"] == "fedgpt-medium"
        assert seen["body"]["tool_choice"] == "auto"

    async def test_finish_reason_is_preserved(self):
        gateway = self._gateway(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "partial"}, "finish_reason": "length"}
                    ]
                },
            )
        )
        turn = await gateway.complete([])
        assert turn.finish_reason == "length"

    async def test_strict_gateway_accepts_the_second_turn_tool_message(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"tool_calls": [_tool_call()]}}]},
                )
            tool_message = body["messages"][-1]
            extra = set(tool_message) - {"role", "content", "tool_call_id"}
            if extra:
                return httpx.Response(400, text=f"unknown fields: {sorted(extra)}")
            return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = HTTPGateway(
            base_url="https://gateway.example/v1",
            api_key="secret",
            model="m",
            client=client,
        )
        try:
            result = await AgentLoop(
                gateway, LocalToolRunner({"get_time": GET_TIME})
            ).run("現在幾點")
        finally:
            await client.aclose()

        assert result.output == "done"
        assert len(requests) == 2

    async def test_omits_tools_when_none_are_offered(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        await self._gateway(handler).complete([{"role": "user", "content": "hi"}])
        assert "tools" not in seen["body"]
        assert "tool_choice" not in seen["body"]

    async def test_trailing_slash_in_base_url_is_tolerated(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        gateway = HTTPGateway(
            base_url="https://gateway.example/v1/",
            api_key="",
            model="m",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await gateway.complete([])
        assert seen["url"] == "https://gateway.example/v1/chat/completions"

    async def test_http_error_carries_the_body(self):
        gateway = self._gateway(lambda r: httpx.Response(500, text="upstream exploded"))
        with pytest.raises(GatewayError, match="upstream exploded"):
            await gateway.complete([])

    async def test_non_json_body(self):
        gateway = self._gateway(lambda r: httpx.Response(200, text="<html>nope</html>"))
        with pytest.raises(GatewayError, match="non-JSON"):
            await gateway.complete([])

    async def test_empty_choices(self):
        gateway = self._gateway(lambda r: httpx.Response(200, json={"choices": []}))
        with pytest.raises(GatewayError, match="no choices"):
            await gateway.complete([])

    async def test_transport_failure(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        with pytest.raises(GatewayError, match="gateway request failed"):
            await self._gateway(handler).complete([])

    def test_missing_base_url_is_rejected_at_construction(self):
        with pytest.raises(GatewayError, match="GATEWAY_BASE_URL"):
            HTTPGateway(base_url="", api_key="", model="m")

    def test_plain_http_is_allowed_only_for_loopback(self):
        with pytest.raises(GatewayError, match="HTTPS"):
            HTTPGateway(base_url="http://gateway.example/v1", api_key="secret", model="m")
        gateway = HTTPGateway(base_url="http://127.0.0.1:8000/v1", api_key="secret", model="m")
        assert gateway.base_url.startswith("http://127.0.0.1")

    def test_invalid_port_is_rejected_at_construction(self):
        with pytest.raises(GatewayError, match="valid port"):
            HTTPGateway(base_url="https://gateway.example:not-a-port/v1", api_key="", model="m")

    async def test_invalid_url_from_http_client_is_normalized(self):
        def handler(request):
            raise httpx.InvalidURL("unsafe detail")

        with pytest.raises(GatewayError, match="gateway URL is invalid"):
            await self._gateway(handler).complete([])

    @pytest.mark.parametrize(
        "payload",
        [[], {"choices": [None]}, {"choices": [{}]}, {"choices": [{"message": "bad"}]}],
    )
    async def test_malformed_response_envelope_is_a_gateway_error(self, payload):
        gateway = self._gateway(lambda request: httpx.Response(200, json=payload))
        with pytest.raises(GatewayError):
            await gateway.complete([])

    def test_configured_reads_the_environment(self, monkeypatch):
        monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
        assert HTTPGateway.configured() is False
        monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example/v1")
        assert HTTPGateway.configured() is True
