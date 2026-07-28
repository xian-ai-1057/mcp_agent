"""Unit tests for `agent.loop` (spec 003 §5)."""

import pytest

from agent.gateway import AssistantTurn, ToolCall
from agent.loop import AgentLoop, TranslationSelfCheck
from agent.mcp_client import LocalToolRunner
from agent.testing import RuleBasedGateway, ScriptedGateway
from contracts.agent import Initiator, StopReason
from tools.base import ToolSpec, object_schema


@pytest.fixture
def runner(all_specs):
    return LocalToolRunner(all_specs)


@pytest.fixture
def phase1_runner(phase1_specs):
    return LocalToolRunner(phase1_specs)


def call(name, arguments=None, call_id="c1"):
    return AssistantTurn(tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments or {}),))


class TestBasicLoop:
    async def test_plain_answer_uses_one_turn_and_no_tools(self, runner):
        gateway = ScriptedGateway([AssistantTurn(content="42")])
        result = await AgentLoop(gateway, runner, self_check=None).run("問題")

        assert result.output == "42"
        assert result.metrics.turns == 1
        assert result.metrics.tool_calls == 0
        assert result.metrics.called_any_tool is False
        assert result.metrics.stop_reason is StopReason.COMPLETED

    async def test_one_tool_call_then_an_answer(self, runner):
        gateway = ScriptedGateway([call("get_time"), AssistantTurn(content="現在是中午")])
        result = await AgentLoop(gateway, runner, self_check=None).run("幾點了")

        assert result.output == "現在是中午"
        assert result.metrics.turns == 2
        assert result.metrics.tool_calls == 1
        assert result.metrics.tool_names == ["get_time"]
        assert result.tool_calls[0].ok is True

    async def test_several_calls_in_one_turn_are_all_executed(self, runner):
        gateway = ScriptedGateway(
            [
                AssistantTurn(
                    tool_calls=(
                        ToolCall(id="a", name="get_time"),
                        ToolCall(id="b", name="say_hello", arguments={"name": "Alex"}),
                    )
                ),
                AssistantTurn(content="done"),
            ]
        )
        result = await AgentLoop(gateway, runner, self_check=None).run("問候並報時")
        assert [r.name for r in result.tool_calls] == ["get_time", "say_hello"]

    async def test_the_system_prompt_leads_the_conversation(self, runner):
        gateway = ScriptedGateway([AssistantTurn(content="ok")])
        await AgentLoop(gateway, runner, self_check=None).run("問題")
        messages = gateway.requests[0]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": "問題"}

    async def test_tools_are_offered_on_every_turn(self, runner):
        gateway = ScriptedGateway([call("get_time"), AssistantTurn(content="ok")])
        await AgentLoop(gateway, runner, self_check=None).run("幾點了")
        assert all(request["tools"] for request in gateway.requests)


class TestFailureHandling:
    async def test_a_failing_tool_is_reported_to_the_model_not_raised(self, runner):
        gateway = ScriptedGateway(
            [
                call("get_time", {"timezone": "Mars/Olympus"}),
                AssistantTurn(content="抱歉，那個時區不存在"),
            ]
        )
        result = await AgentLoop(gateway, runner, self_check=None).run("火星幾點")

        assert result.output == "抱歉，那個時區不存在"
        assert result.tool_calls[0].ok is False
        assert "unknown timezone" in result.tool_calls[0].error
        tool_message = gateway.requests[1]["messages"][-1]
        assert tool_message["role"] == "tool"
        assert "Error" in tool_message["content"]

    async def test_an_unknown_tool_name_is_reported_not_raised(self, runner):
        gateway = ScriptedGateway([call("no_such_tool"), AssistantTurn(content="recovered")])
        result = await AgentLoop(gateway, runner, self_check=None).run("問題")
        assert result.output == "recovered"
        assert result.tool_calls[0].ok is False

    async def test_malformed_arguments_never_reach_the_tool(self, runner):
        broken = AssistantTurn(
            tool_calls=(
                ToolCall(
                    id="c1",
                    name="get_time",
                    arguments={},
                    raw_arguments="{not json",
                    parse_error="arguments were not valid JSON",
                ),
            )
        )
        gateway = ScriptedGateway([broken, AssistantTurn(content="recovered")])
        result = await AgentLoop(gateway, runner, self_check=None).run("問題")

        assert result.output == "recovered"
        assert result.tool_calls[0].ok is False
        assert "not valid JSON" in result.tool_calls[0].error

    async def test_a_crashing_handler_becomes_a_tool_error(self):
        def explode(arguments):
            raise RuntimeError("boom")

        spec = ToolSpec(
            name="explodes", description="call to explode", input_schema=object_schema({}), handler=explode
        )
        gateway = ScriptedGateway([call("explodes"), AssistantTurn(content="recovered")])
        result = await AgentLoop(gateway, LocalToolRunner({"explodes": spec}), self_check=None).run("go")

        assert result.output == "recovered"
        assert "boom" in result.tool_calls[0].error


class TestTurnBudget:
    async def test_max_turns_stops_a_model_that_only_calls_tools(self, runner):
        gateway = ScriptedGateway([call("get_time", call_id=f"c{i}") for i in range(20)])
        result = await AgentLoop(gateway, runner, max_turns=4, self_check=None).run("幾點了")

        assert result.metrics.turns == 4
        assert result.metrics.stop_reason is StopReason.MAX_TURNS
        assert result.metrics.tool_calls == 4

    async def test_the_last_text_survives_exhaustion(self, runner):
        gateway = ScriptedGateway(
            [
                AssistantTurn(content="部分答案", tool_calls=(ToolCall(id="a", name="get_time"),)),
                AssistantTurn(tool_calls=(ToolCall(id="b", name="get_time"),)),
            ]
        )
        result = await AgentLoop(gateway, runner, max_turns=2, self_check=None).run("幾點了")
        assert result.output == "部分答案"
        assert result.metrics.stop_reason is StopReason.MAX_TURNS


class TestNoToolRun:
    async def test_a_model_that_ignores_tools_completes_and_is_counted(self, runner):
        """Not an error. The plan refuses to assume tool use, so it is measured."""
        gateway = RuleBasedGateway(use_tools=False)
        result = await AgentLoop(gateway, runner).run("請幫我翻譯：客戶申請提高臨時額度")

        assert result.output
        assert result.metrics.called_any_tool is False
        assert result.metrics.tool_calls == 0
        assert result.verify is None
        assert result.metrics.stop_reason is StopReason.COMPLETED


class TestSelfCheckPolicy:
    def test_applies_only_when_lookup_ran_and_verify_exists(self):
        policy = TranslationSelfCheck()
        both = {"lookup_terms", "verify_translation", "get_time"}
        assert policy.applies({"lookup_terms"}, both) is True
        assert policy.applies({"get_time"}, both) is False
        assert policy.applies({"lookup_terms"}, {"lookup_terms", "get_time"}) is False

    async def test_it_triggers_on_tool_use_not_on_wording(self, runner):
        """A request full of translation words but no lookup gets no self-check."""
        gateway = ScriptedGateway([call("get_time"), AssistantTurn(content="請幫我翻譯這句話")])
        result = await AgentLoop(gateway, runner).run("請幫我翻譯：客戶申請提高臨時額度")
        assert result.verify is None

    async def test_a_clean_translation_is_verified_but_not_retranslated(self, runner):
        result = await AgentLoop(RuleBasedGateway(), runner).run("請翻譯：客戶申請提高臨時額度")

        assert result.verify is not None
        assert result.verify.hit_rate == 1.0
        assert result.metrics.retranslations == 0
        assert "temporary credit limit" in result.output

    async def test_verify_calls_are_recorded_as_policy_not_model(self, runner):
        result = await AgentLoop(RuleBasedGateway(), runner).run("請翻譯：客戶申請提高臨時額度")

        initiators = {r.name: r.initiator for r in result.tool_calls}
        assert initiators["lookup_terms"] is Initiator.MODEL
        assert initiators["verify_translation"] is Initiator.POLICY
        # The headline metric must reflect what the *model* chose to do.
        assert result.metrics.tool_calls == 1
        assert result.metrics.tool_names == ["lookup_terms"]

    async def test_a_dropped_term_is_repaired(self, runner):
        gateway = RuleBasedGateway(glossary_fidelity=0.0, repair_fidelity=1.0)
        result = await AgentLoop(gateway, runner).run("請翻譯：外幣帳戶的牌告利率每日更新")

        assert result.metrics.retranslations == 1
        assert result.verify.hit_rate == 1.0
        assert "foreign currency account" in result.output
        assert "posted interest rate" in result.output

    async def test_the_cap_stops_a_model_that_never_improves(self, runner):
        gateway = RuleBasedGateway(glossary_fidelity=0.0, repair_fidelity=0.0)
        policy = TranslationSelfCheck(max_retranslate=2)
        result = await AgentLoop(gateway, runner, self_check=policy).run("請翻譯：臨時額度")

        assert result.metrics.retranslations == 2
        assert result.verify.hit_rate < 1.0

    async def test_repair_turns_offer_no_tools(self, runner):
        gateway = RuleBasedGateway(glossary_fidelity=0.0, repair_fidelity=1.0)
        await AgentLoop(gateway, runner).run("請翻譯：外幣帳戶的牌告利率每日更新")

        repair_requests = [
            request
            for request in gateway.requests
            if request["messages"][-1]["role"] == "user"
            and "請修正上述術語" in request["messages"][-1]["content"]
        ]
        assert repair_requests
        assert all(request["tools"] is None for request in repair_requests)

    async def test_a_worse_repair_is_discarded(self, runner, monkeypatch):
        """Never hand back a translation worse than the one already in hand."""
        gateway = RuleBasedGateway(glossary_fidelity=0.5, repair_fidelity=1.0)
        original_repair = gateway._repair
        monkeypatch.setattr(gateway, "_repair", lambda prompt: "Nothing useful at all.")

        result = await AgentLoop(gateway, runner).run("請翻譯：外幣帳戶的牌告利率每日更新")
        assert original_repair  # kept for clarity about what was replaced
        assert "foreign currency account" in result.output
        assert result.verify.hit_rate == 0.5

    async def test_self_check_can_be_disabled(self, runner):
        result = await AgentLoop(RuleBasedGateway(), runner, self_check=None).run(
            "請翻譯：客戶申請提高臨時額度"
        )
        assert result.verify is None
        assert all(r.initiator is Initiator.MODEL for r in result.tool_calls)


class TestFromEnv:
    def test_reads_the_budgets(self, runner, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_TURNS", "9")
        monkeypatch.setenv("AGENT_MAX_RETRANSLATE", "4")
        loop = AgentLoop.from_env(RuleBasedGateway(), runner)
        assert loop.max_turns == 9
        assert loop.self_check.max_retranslate == 4

    def test_overrides_win(self, runner, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_TURNS", "9")
        loop = AgentLoop.from_env(RuleBasedGateway(), runner, max_turns=2, self_check=None)
        assert loop.max_turns == 2
        assert loop.self_check is None
