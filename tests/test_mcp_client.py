"""Hermetic tests for one-server MCP transport behaviour."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

import agent.mcp_client as client_module
from agent.mcp_client import MCPToolClient, ToolInvocationError, _result_text


class FakeSession:
    def __init__(self, read, write, **kwargs):
        self.constructor_kwargs = kwargs
        self.call_kwargs = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=[])

    async def call_tool(self, name, arguments, **kwargs):
        self.call_kwargs = kwargs
        return SimpleNamespace(
            structuredContent={"name": name, "arguments": arguments},
            content=[],
            isError=False,
        )


def install_fake_transport(monkeypatch, session_type=FakeSession):
    captured = SimpleNamespace(params=None, session=None, transport_closed=False)

    @asynccontextmanager
    async def fake_stdio(params, *args):
        captured.params = params
        try:
            yield object(), object()
        finally:
            captured.transport_closed = True

    def session_factory(read, write, **kwargs):
        captured.session = session_type(read, write, **kwargs)
        return captured.session

    monkeypatch.setattr(client_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(client_module, "ClientSession", session_factory)
    return captured


class TestChildEnvironment:
    async def test_only_runtime_explicit_and_named_ambient_values_cross_boundary(
        self, monkeypatch
    ):
        monkeypatch.setenv("PATH", "/runtime/bin")
        monkeypatch.setenv("GATEWAY_API_KEY", "must-not-leak")
        monkeypatch.setenv("UNRELATED_SECRET", "also-must-not-leak")
        monkeypatch.setenv("CAPABILITY_TOKEN", "explicitly-requested")
        captured = install_fake_transport(monkeypatch)

        async with MCPToolClient(
            command="fake",
            args=["server.py"],
            env={"SERVER_SETTING": "yes"},
            inherit_env=("CAPABILITY_TOKEN",),
        ):
            pass

        child_env = captured.params.env
        assert child_env["PATH"] == "/runtime/bin"
        assert child_env["CAPABILITY_TOKEN"] == "explicitly-requested"
        assert child_env["SERVER_SETTING"] == "yes"
        assert "GATEWAY_API_KEY" not in child_env
        assert "UNRELATED_SECRET" not in child_env
        assert str(client_module.REPO_ROOT) in child_env["PYTHONPATH"]

    async def test_legacy_root_client_inherits_only_its_known_tool_settings(
        self, monkeypatch
    ):
        monkeypatch.setenv("GLOSSARY_CSV", "/data/glossary.csv")
        monkeypatch.setenv("WEATHER_PROVIDER", "mock")
        monkeypatch.setenv("GATEWAY_API_KEY", "must-not-leak")
        captured = install_fake_transport(monkeypatch)

        async with MCPToolClient():
            pass

        assert captured.params.env["GLOSSARY_CSV"] == "/data/glossary.csv"
        assert captured.params.env["WEATHER_PROVIDER"] == "mock"
        assert "GATEWAY_API_KEY" not in captured.params.env

    async def test_explicit_env_can_intentionally_override_runtime_value(self, monkeypatch):
        monkeypatch.setenv("PATH", "/parent/bin")
        captured = install_fake_transport(monkeypatch)

        async with MCPToolClient(command="fake", env={"PATH": "/server/bin"}):
            pass

        assert captured.params.env["PATH"] == "/server/bin"


class TestTimeoutsAndErrors:
    async def test_read_and_call_timeouts_reach_the_sdk(self, monkeypatch):
        captured = install_fake_transport(monkeypatch)

        async with MCPToolClient(
            command="fake",
            inherit_env=(),
            read_timeout_seconds=7,
            call_timeout_seconds=11,
        ) as client:
            result = await client.call("echo", {"value": 1})

        assert captured.session.constructor_kwargs["read_timeout_seconds"] == timedelta(seconds=7)
        assert captured.session.call_kwargs["read_timeout_seconds"] == timedelta(seconds=11)
        assert '"value": 1' in result

    async def test_transport_failures_are_recoverable_tool_errors(self):
        class BrokenSession:
            async def call_tool(self, name, arguments, **kwargs):
                raise OSError("pipe closed")

        client = MCPToolClient()
        client._session = BrokenSession()

        with pytest.raises(ToolInvocationError, match="transport failed"):
            await client.call("lookup", {})

    async def test_cancellation_is_not_converted_to_a_tool_error(self):
        class CancelledSession:
            async def call_tool(self, name, arguments, **kwargs):
                raise asyncio.CancelledError

        client = MCPToolClient()
        client._session = CancelledSession()

        with pytest.raises(asyncio.CancelledError):
            await client.call("lookup", {})

    @pytest.mark.parametrize("value", [0, -1, "not-a-number"])
    def test_invalid_timeouts_fail_at_construction(self, value):
        with pytest.raises(ValueError, match="timeout"):
            MCPToolClient(read_timeout_seconds=value)


class TestLifecycle:
    async def test_initialize_failure_closes_transport_and_resets_client(self, monkeypatch):
        class FailingSession(FakeSession):
            async def initialize(self):
                raise OSError("bad handshake")

        captured = install_fake_transport(monkeypatch, FailingSession)
        client = MCPToolClient(command="fake", inherit_env=())

        with pytest.raises(ToolInvocationError, match="failed to connect"):
            async with client:
                pytest.fail("unreachable")

        assert captured.session.closed
        assert captured.transport_closed
        assert client._stack is None
        assert client._session is None
        assert client.mcp_tools == []

    async def test_call_before_connect_preserves_the_old_usage_error(self):
        with pytest.raises(RuntimeError, match="async context manager"):
            await MCPToolClient().call("anything", {})


class TestToolDiscovery:
    async def test_collects_every_paginated_tools_page(self, monkeypatch):
        class PaginatedSession(FakeSession):
            async def list_tools(self, cursor=None):
                if cursor is None:
                    return SimpleNamespace(
                        tools=[SimpleNamespace(name="first")],
                        nextCursor="page-2",
                    )
                assert cursor == "page-2"
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="second")],
                    nextCursor=None,
                )

        install_fake_transport(monkeypatch, PaginatedSession)
        async with MCPToolClient(command="fake", inherit_env=()) as client:
            assert [tool.name for tool in client.mcp_tools] == ["first", "second"]

    async def test_cursor_cycle_fails_and_closes_the_session(self, monkeypatch):
        class CyclingSession(FakeSession):
            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[], nextCursor="same")

        captured = install_fake_transport(monkeypatch, CyclingSession)
        client = MCPToolClient(command="fake", inherit_env=())
        with pytest.raises(ToolInvocationError, match="cursor cycle"):
            async with client:
                pytest.fail("unreachable")

        assert captured.session.closed
        assert captured.transport_closed
        assert client.mcp_tools == []


class TestResultMapping:
    def test_empty_structured_content_is_not_discarded(self):
        result = SimpleNamespace(structuredContent={}, content=[])
        assert _result_text(result) == "{}"

    def test_text_blocks_are_joined(self):
        result = SimpleNamespace(
            structuredContent=None,
            content=[
                SimpleNamespace(type="text", text="one"),
                SimpleNamespace(type="text", text="two"),
            ],
        )
        assert _result_text(result) == "one\ntwo"

    def test_non_text_blocks_are_never_reported_as_empty_success(self):
        result = SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(type="image", data="...")],
        )
        with pytest.raises(ToolInvocationError, match="non-text.*image"):
            _result_text(result)
