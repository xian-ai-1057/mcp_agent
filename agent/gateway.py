"""OpenAI-compatible chat completions over the gateway.

The plan names gateway format drift as the top integration risk, so the response
reader below is written to be forgiving in every direction a real gateway has
been observed to differ — and each tolerance has a unit test with the payload
shape that motivated it. Discovering a new quirk should mean adding a fixture,
not editing the agent loop.

See `specs/003-agent-client/spec.md` §2.
"""

import ipaddress
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


class GatewayError(Exception):
    """The gateway could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class ToolCall:
    """One tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None
    protocol: str = "tool_calls"


@dataclass(frozen=True)
class AssistantTurn:
    """One assistant message: text, tool calls, or both."""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    refusal: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Gateway(Protocol):
    """The only thing the loop needs from a model provider."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> AssistantTurn: ...


def _coerce_arguments(raw: Any) -> tuple[dict[str, Any], str, str | None]:
    """Normalise `function.arguments` into a dict.

    Per the OpenAI schema this is a JSON *string*, but several gateways emit an
    already-decoded object, and some emit `""` for a no-argument call. All three
    are accepted; anything else is recorded as a parse error rather than raised,
    so one malformed call degrades to a tool error the model can recover from
    instead of killing the run.
    """
    if raw is None or raw == "":
        return {}, "" if raw is None else str(raw), None
    if isinstance(raw, dict):
        return raw, json.dumps(raw, ensure_ascii=False), None
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, raw, f"arguments were not valid JSON: {exc}"
        if isinstance(decoded, dict):
            return decoded, raw, None
        return {}, raw, f"arguments must decode to an object, got {type(decoded).__name__}"
    return {}, str(raw), f"unsupported arguments type {type(raw).__name__}"


def _parse_tool_call(
    entry: dict[str, Any], index: int, *, protocol: str = "tool_calls"
) -> ToolCall:
    function = entry.get("function")
    if not isinstance(function, dict):
        raise GatewayError(f"gateway tool call {index} has no function object")
    name = function.get("name") or entry.get("name")
    if not isinstance(name, str) or not name:
        raise GatewayError(f"gateway tool call {index} has no function name")
    arguments, raw, error = _coerce_arguments(function.get("arguments"))
    return ToolCall(
        id=str(entry.get("id") or f"call_{index}_{uuid.uuid4().hex[:8]}"),
        name=name,
        arguments=arguments,
        raw_arguments=raw,
        parse_error=error,
        protocol=protocol,
    )


def parse_assistant_message(
    message: Any,
    *,
    finish_reason: str | None = None,
) -> AssistantTurn:
    """Read one `choices[0].message` into an `AssistantTurn`."""
    if not isinstance(message, dict):
        raise GatewayError("gateway choice message must be an object")
    raw_calls = message.get("tool_calls")
    calls: list[ToolCall] = []

    if raw_calls is not None:
        if not isinstance(raw_calls, list):
            raise GatewayError("gateway tool_calls must be a list")
        for index, entry in enumerate(raw_calls):
            if not isinstance(entry, dict):
                raise GatewayError(f"gateway tool call {index} must be an object")
            calls.append(_parse_tool_call(entry, index))
    elif isinstance(message.get("function_call"), dict):
        # Legacy single-call shape, still emitted by some gateways.
        calls = [
            _parse_tool_call(
                {"function": message["function_call"]}, 0, protocol="function_call"
            )
        ]
    elif message.get("function_call") is not None:
        raise GatewayError("gateway function_call must be an object")

    refusal = message.get("refusal")
    if refusal is not None and not isinstance(refusal, str):
        raise GatewayError("gateway message refusal must be a string")

    content = message.get("content")
    if isinstance(content, list):
        # Some gateways return content parts rather than a bare string.
        text_parts: list[str] = []
        refusal_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            part_refusal = part.get("refusal")
            if isinstance(part_refusal, str):
                refusal_parts.append(part_refusal)
        content = "".join(text_parts)
        if refusal is None and refusal_parts:
            refusal = "".join(refusal_parts)

    return AssistantTurn(
        content=content if isinstance(content, str) else None,
        tool_calls=tuple(calls),
        finish_reason=finish_reason,
        refusal=refusal,
        raw=message,
    )


class HTTPGateway:
    """`Gateway` backed by an OpenAI-compatible `/chat/completions` endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise GatewayError("GATEWAY_BASE_URL is not set")
        parsed = urlsplit(base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GatewayError("GATEWAY_BASE_URL must not contain credentials, query, or fragment")
        if not parsed.hostname:
            raise GatewayError("GATEWAY_BASE_URL must include a host")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise GatewayError("GATEWAY_BASE_URL must contain a valid port") from exc
        loopback = parsed.hostname.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise GatewayError(
                "GATEWAY_BASE_URL must use HTTPS (HTTP is allowed only for loopback)"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> "HTTPGateway":
        return cls(
            base_url=os.environ.get("GATEWAY_BASE_URL", ""),
            api_key=os.environ.get("GATEWAY_API_KEY", ""),
            model=os.environ.get("GATEWAY_MODEL", "fedgpt-medium"),
            timeout=float(os.environ.get("GATEWAY_TIMEOUT", "60")),
        )

    @staticmethod
    def configured() -> bool:
        """True when the environment carries enough to reach a real gateway."""
        return bool(os.environ.get("GATEWAY_BASE_URL"))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> AssistantTurn:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = await self._http().post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
        except httpx.InvalidURL as exc:
            raise GatewayError("gateway URL is invalid") from exc
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway request failed: {exc}") from exc

        if response.status_code >= 400:
            raise GatewayError(
                f"gateway returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayError(f"gateway returned non-JSON body: {response.text[:500]}") from exc

        if not isinstance(body, dict):
            raise GatewayError("gateway JSON response must be an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GatewayError(f"gateway response contained no choices: {body}")
        if not isinstance(choices[0], dict):
            raise GatewayError("gateway choice must be an object")
        if "message" not in choices[0]:
            raise GatewayError("gateway choice contained no message")
        finish_reason = choices[0].get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise GatewayError("gateway finish_reason must be a string")
        return parse_assistant_message(
            choices[0]["message"], finish_reason=finish_reason
        )
