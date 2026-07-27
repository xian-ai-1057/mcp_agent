"""OpenAI-compatible chat completions over the gateway.

The plan names gateway format drift as the top integration risk, so the response
reader below is written to be forgiving in every direction a real gateway has
been observed to differ — and each tolerance has a unit test with the payload
shape that motivated it. Discovering a new quirk should mean adding a fixture,
not editing the agent loop.

See `specs/003-agent-client/spec.md` §2.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

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


@dataclass(frozen=True)
class AssistantTurn:
    """One assistant message: text, tool calls, or both."""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
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


def _parse_tool_call(entry: dict[str, Any], index: int) -> ToolCall:
    function = entry.get("function") or {}
    arguments, raw, error = _coerce_arguments(function.get("arguments"))
    return ToolCall(
        id=str(entry.get("id") or f"call_{index}_{uuid.uuid4().hex[:8]}"),
        name=str(function.get("name") or entry.get("name") or ""),
        arguments=arguments,
        raw_arguments=raw,
        parse_error=error,
    )


def parse_assistant_message(message: dict[str, Any]) -> AssistantTurn:
    """Read one `choices[0].message` into an `AssistantTurn`."""
    raw_calls = message.get("tool_calls")
    calls: list[ToolCall] = []

    if isinstance(raw_calls, list):
        calls = [
            _parse_tool_call(entry, index)
            for index, entry in enumerate(raw_calls)
            if isinstance(entry, dict)
        ]
    elif isinstance(message.get("function_call"), dict):
        # Legacy single-call shape, still emitted by some gateways.
        calls = [_parse_tool_call({"function": message["function_call"]}, 0)]

    content = message.get("content")
    if isinstance(content, list):
        # Some gateways return content parts rather than a bare string.
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )

    return AssistantTurn(
        content=content if isinstance(content, str) else None,
        tool_calls=tuple(call for call in calls if call.name),
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

        choices = body.get("choices")
        if not choices:
            raise GatewayError(f"gateway response contained no choices: {body}")

        return parse_assistant_message(choices[0].get("message") or {})
