"""Hermetic tests for configuration and multi-server MCP dispatch."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from agent.mcp_client import MCPToolPool, ToolInvocationError
from agent.mcp_config import MCPConfigError, MCPServerConfig, load_mcp_server_configs


def tool(name: str):
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        inputSchema={"type": "object", "properties": {}},
    )


class FakeClient:
    def __init__(self, state, **kwargs):
        self.state = state
        self.command = kwargs["command"]
        self.mcp_tools = state["tools"].get(self.command, [])
        self.closed = False

    async def __aenter__(self):
        error = self.state.get("start_errors", {}).get(self.command)
        if error is not None:
            raise error
        self.state.setdefault("entered", []).append(self.command)
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        self.state.setdefault("closed", []).append(self.command)

    async def call(self, name, arguments):
        error = self.state.get("call_errors", {}).get(self.command)
        if error is not None:
            raise error
        self.state.setdefault("calls", []).append((self.command, name, arguments))
        return f"{self.command}:{name}"


def factory(state):
    return lambda **kwargs: FakeClient(state, **kwargs)


class TestAggregation:
    async def test_lists_namespaced_tools_and_dispatches_to_remote_names(self):
        state = {"tools": {"alpha-cmd": [tool("search")], "beta-cmd": [tool("search")]}}
        configs = [
            MCPServerConfig(name="alpha", command="alpha-cmd"),
            MCPServerConfig(name="beta", command="beta-cmd", tool_prefix="kb"),
        ]

        async with MCPToolPool(configs, client_factory=factory(state)) as pool:
            assert pool.tool_names == {"search", "kb__search"}
            assert {item["function"]["name"] for item in pool.openai_tools} == pool.tool_names
            assert await pool.call("search", {"q": "a"}) == "alpha-cmd:search"
            assert await pool.call("kb__search", {"q": "b"}) == "beta-cmd:search"

        assert state["calls"] == [
            ("alpha-cmd", "search", {"q": "a"}),
            ("beta-cmd", "search", {"q": "b"}),
        ]
        assert state["closed"] == ["beta-cmd", "alpha-cmd"]

    async def test_unprefixed_collision_fails_startup_and_closes_started_servers(self):
        state = {"tools": {"one": [tool("search")], "two": [tool("search")]}}
        configs = [
            MCPServerConfig(name="one", command="one"),
            MCPServerConfig(name="two", command="two"),
        ]
        pool = MCPToolPool(configs, client_factory=factory(state))

        with pytest.raises(MCPConfigError, match="collision.*tool_prefix"):
            async with pool:
                pytest.fail("unreachable")

        assert state["closed"] == ["two", "one"]
        assert pool.tool_names == set()

    async def test_namespaced_public_name_must_fit_gateway_limit(self):
        state = {"tools": {"one": [tool("x" * 60)]}}
        config = MCPServerConfig(name="one", command="one", tool_prefix="prefix")
        with pytest.raises(MCPConfigError, match="gateway-compatible"):
            async with MCPToolPool([config], client_factory=factory(state)):
                pytest.fail("unreachable")

    async def test_required_server_failure_closes_earlier_servers(self):
        state = {
            "tools": {"good": [tool("ok")], "broken": []},
            "start_errors": {"broken": OSError("cannot spawn")},
        }
        configs = [
            MCPServerConfig(name="good", command="good"),
            MCPServerConfig(name="broken", command="broken"),
        ]

        with pytest.raises(ToolInvocationError, match="required MCP server 'broken'"):
            async with MCPToolPool(configs, client_factory=factory(state)):
                pytest.fail("unreachable")

        assert state["closed"] == ["good"]

    async def test_optional_server_failure_is_observable_and_other_tools_remain(self):
        state = {
            "tools": {"good": [tool("ok")], "optional": []},
            "start_errors": {"optional": OSError("offline")},
        }
        configs = [
            MCPServerConfig(name="good", command="good"),
            MCPServerConfig(name="optional", command="optional", required=False),
        ]

        async with MCPToolPool(configs, client_factory=factory(state)) as pool:
            assert pool.tool_names == {"ok"}
            assert "offline" in pool.connection_errors["optional"]

    async def test_unknown_public_name_is_a_tool_error(self):
        state = {"tools": {"one": [tool("known")]}}
        async with MCPToolPool(
            [MCPServerConfig(name="one", command="one")],
            client_factory=factory(state),
        ) as pool:
            with pytest.raises(ToolInvocationError, match="unknown tool"):
                await pool.call("missing", {})

    async def test_call_cancellation_is_not_normalized(self):
        state = {
            "tools": {"one": [tool("slow")]},
            "call_errors": {"one": asyncio.CancelledError()},
        }
        async with MCPToolPool(
            [MCPServerConfig(name="one", command="one")],
            client_factory=factory(state),
        ) as pool:
            with pytest.raises(asyncio.CancelledError):
                await pool.call("slow", {})

    async def test_call_before_enter_is_a_usage_error(self):
        pool = MCPToolPool([MCPServerConfig(name="one", command="one")])
        with pytest.raises(RuntimeError, match="async context manager"):
            await pool.call("anything", {})


class TestConfig:
    def test_current_python_sentinel_uses_the_running_interpreter(self):
        config = MCPServerConfig(name="local", command="{python}")
        assert config.command == sys.executable

    def test_json_loader_normalises_every_supported_field(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "name": "rag",
                            "command": "python",
                            "args": ["-m", "rag_server"],
                            "env": {"MODE": "upload"},
                            "inherit_env": ["RAG_API_TOKEN"],
                            "tool_prefix": "rag",
                            "required": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        [config] = load_mcp_server_configs(path)
        assert config == MCPServerConfig(
            name="rag",
            command="python",
            args=("-m", "rag_server"),
            env={"MODE": "upload"},
            inherit_env=("RAG_API_TOKEN",),
            tool_prefix="rag",
            required=False,
        )

    def test_bare_list_form_is_supported(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text('[{"name":"one","command":"server"}]', encoding="utf-8")
        assert load_mcp_server_configs(path)[0].name == "one"

    @pytest.mark.parametrize(
        "payload,match",
        [
            ({"servers": []}, "at least one"),
            ({"servers": [{"name": "a", "command": "x", "typo": 1}]}, "unknown"),
            (
                {
                    "servers": [
                        {"name": "same", "command": "one"},
                        {"name": "same", "command": "two"},
                    ]
                },
                "duplicate",
            ),
            ({"servers": [{"name": "bad name", "command": "x"}]}, "must match"),
            ({"servers": [{"name": "a", "command": "x", "required": "yes"}]}, "boolean"),
            ({"servers": [{"name": "a", "command": "x", "env": {"N": 1}}]}, "string"),
        ],
    )
    def test_invalid_config_fails_with_context(self, tmp_path, payload, match):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(MCPConfigError, match=match):
            load_mcp_server_configs(path)

    def test_pool_defaults_to_the_legacy_root_server(self):
        pool = MCPToolPool()
        [config] = pool.configs
        assert config.name == "root"
        assert config.command
        assert config.args[0].endswith("server.py")
        assert "GATEWAY_API_KEY" not in config.inherit_env
