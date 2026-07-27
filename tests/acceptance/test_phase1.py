"""Phase 1 — Minimal Demo. Acceptance criteria 1-7."""

import hashlib
import importlib
import json
import sys

import pytest

from agent.gateway import HTTPGateway
from agent.loop import AgentLoop
from agent.mcp_client import LocalToolRunner
from agent.metrics import RunRecord, summarize
from agent.testing import RuleBasedGateway
from contracts.agent import StopReason
from glossary.matcher import match_terms
from glossary.scanner import scan
from tests.mcp_session import mcp_session

DEMO_SENTENCE = "請幫我翻譯：客戶申請提高臨時額度"

requires_gateway = pytest.mark.skipif(
    not HTTPGateway.configured(),
    reason="no GATEWAY_BASE_URL configured — model behaviour cannot be measured",
)


class TestCriterion1LongestMatchFirst:
    """`lookup_terms` matches the golden fixture, and 「額度」 must not appear."""

    def test_matches_the_golden_fixture_exactly(self, all_specs, load_fixture):
        golden = next(
            case
            for case in load_fixture("scan_cases.json")["cases"]
            if case["name"] == "acceptance-1-longest-match-suppresses-short-term"
        )
        assert golden["text"] == DEMO_SENTENCE

        result = all_specs["lookup_terms"].run({"text": DEMO_SENTENCE})
        assert result["matches"] == golden["expect"]

    def test_the_short_term_is_not_injected(self, all_specs):
        result = all_specs["lookup_terms"].run({"text": DEMO_SENTENCE})
        found = {match["zh"] for match in result["matches"]}
        assert "額度" not in found
        assert found == {"臨時額度"}
        assert "額度 →" not in result["glossary_block"].replace("臨時額度 →", "")


class TestCriterion2LoopBudget:
    """The loop finishes in ≤3 turns, having made exactly 1 tool call."""

    async def test_with_the_phase_1_tool_set(self, phase1_specs):
        loop = AgentLoop(RuleBasedGateway(), LocalToolRunner(phase1_specs))
        result = await loop.run(DEMO_SENTENCE)

        assert result.metrics.turns <= 3
        assert result.metrics.tool_calls == 1
        assert result.metrics.tool_names == ["lookup_terms"]
        assert result.metrics.stop_reason is StopReason.COMPLETED

    async def test_still_holds_against_the_live_server_with_every_tool(self):
        """The budget must not degrade once the registry grows."""
        async with mcp_session() as server:
            result = await AgentLoop(RuleBasedGateway(), server).run(DEMO_SENTENCE)

        assert result.metrics.turns <= 3
        assert result.metrics.tool_calls == 1
        assert result.metrics.stop_reason is StopReason.COMPLETED


class TestCriterion3EndToEndTranslationHits:
    """The English produced end to end is judged HIT by the matcher."""

    async def test_through_the_real_mcp_server(self, glossary):
        async with mcp_session() as server:
            result = await AgentLoop(RuleBasedGateway(), server).run(DEMO_SENTENCE)

        verdicts = match_terms(result.output, scan(DEMO_SENTENCE, glossary), glossary)
        assert [v.verdict.value for v in verdicts] == ["HIT"]
        assert result.verify is not None and result.verify.hit_rate == 1.0


class TestCriterion4NoToolCallIsSurvivable:
    """A model that calls nothing must not break the run, and must be counted."""

    async def test_the_run_completes_and_is_recorded_as_no_tool(self, all_specs):
        gateway = RuleBasedGateway(use_tools=False)
        result = await AgentLoop(gateway, LocalToolRunner(all_specs)).run(DEMO_SENTENCE)

        assert result.output
        assert result.metrics.called_any_tool is False
        assert result.metrics.tool_calls == 0
        assert result.metrics.stop_reason is StopReason.COMPLETED

    async def test_the_report_separates_these_runs(self, all_specs):
        runner = LocalToolRunner(all_specs)
        skipped = await AgentLoop(RuleBasedGateway(use_tools=False), runner).run(DEMO_SENTENCE)
        used = await AgentLoop(RuleBasedGateway(), runner).run(DEMO_SENTENCE)

        report = summarize(
            [RunRecord(DEMO_SENTENCE, skipped), RunRecord(DEMO_SENTENCE, used)]
        )
        assert report.tool_call_rate == 0.5


class TestCriterion5ToolSelection:
    """10 fixture sentences, ≥90% routed to the right tool."""

    async def _measure(self, gateway, cases, tools):
        records = []
        for case in cases:
            result = await AgentLoop(gateway, tools).run(case["text"])
            records.append(RunRecord(case["text"], result, expected_tool=case["expect_tool"]))
        return summarize(records)

    async def test_the_measurement_works(self, load_fixture, phase1_specs):
        """Harness check: the double routes 10/10, so the metric is exercised."""
        cases = load_fixture("routing_cases.json")["cases"]
        assert len(cases) == 10

        report = await self._measure(RuleBasedGateway(), cases, LocalToolRunner(phase1_specs))
        assert report.runs == 10
        assert report.tool_selection_accuracy == 1.0
        assert report.tool_call_rate == 1.0

    async def test_a_misroute_is_detected(self, load_fixture, phase1_specs):
        """The metric must be capable of failing, or it proves nothing."""
        cases = load_fixture("routing_cases.json")["cases"]
        swapped = [{**case, "expect_tool": "say_hello"} for case in cases]
        report = await self._measure(RuleBasedGateway(), swapped, LocalToolRunner(phase1_specs))
        assert report.tool_selection_accuracy == 0.0
        assert len(report.routing_errors) == 10

    @requires_gateway
    async def test_the_model_routes_correctly(self, load_fixture):
        cases = load_fixture("routing_cases.json")["cases"]
        gateway = HTTPGateway.from_env()
        try:
            async with mcp_session() as server:
                report = await self._measure(gateway, cases, server)
        finally:
            await gateway.aclose()

        assert report.tool_selection_accuracy >= 0.9, report.routing_errors


PROBE_MODULE = "probe_added_by_acceptance_test"
PROBE_SOURCE = '''"""Throwaway tool written by acceptance criterion 6. Safe to delete."""

from tools.base import ToolSpec, object_schema


def _run(arguments):
    return {"ok": True, "note": "registered without touching server.py"}


SPEC = ToolSpec(
    name="probe_tool",
    description="A probe tool that does nothing. Call this only in tests.",
    input_schema=object_schema({}),
    handler=_run,
)
'''


class TestCriterion6Pluggability:
    """A new tool file appears on restart, with `server.py` and the prompt untouched.

    The design claim is "adding a tool is adding a file". This is the test that
    keeps it from being a slogan.
    """

    @staticmethod
    def _digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    async def test_a_new_file_registers_itself(self, repo_root):
        server_py = repo_root / "server.py"
        prompts_py = repo_root / "agent" / "prompts.py"
        before = (self._digest(server_py), self._digest(prompts_py))

        async with mcp_session() as server:
            assert "probe_tool" not in server.tool_names

        probe = repo_root / "tools" / f"{PROBE_MODULE}.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        importlib.invalidate_caches()
        try:
            # A fresh session is a fresh server process — i.e. a restart.
            async with mcp_session() as server:
                assert "probe_tool" in server.tool_names
                payload = json.loads(await server.call("probe_tool", {}))
                assert payload["ok"] is True
        finally:
            probe.unlink()
            sys.modules.pop(f"tools.{PROBE_MODULE}", None)
            importlib.invalidate_caches()

        after = (self._digest(server_py), self._digest(prompts_py))
        assert after == before, "adding a tool must not require editing server.py or the prompt"

    async def test_the_tool_is_gone_once_the_file_is(self, repo_root):
        async with mcp_session() as server:
            assert "probe_tool" not in server.tool_names


class TestCriterion7GlossaryReloadWithoutRestart:
    """An edited CSV is visible to a *running* server."""

    async def test_new_terms_appear_mid_session(self, tmp_path):
        header = "zh,en,aliases,category\n"
        csv = tmp_path / "glossary.csv"
        csv.write_text(header + "額度,credit limit,,授信\n", encoding="utf-8")

        async with mcp_session(env={"GLOSSARY_CSV": str(csv)}) as server:
            before = json.loads(await server.call("lookup_terms", {"text": "申請臨時額度"}))
            assert [m["zh"] for m in before["matches"]] == ["額度"]

            csv.write_text(
                header + "額度,credit limit,,授信\n臨時額度,temporary credit limit,,授信\n",
                encoding="utf-8",
            )

            # Same server process, no restart, no notification.
            after = json.loads(await server.call("lookup_terms", {"text": "申請臨時額度"}))
            assert [m["zh"] for m in after["matches"]] == ["臨時額度"]
            assert after["glossary_block"] == "- 臨時額度 → temporary credit limit"

    async def test_a_broken_edit_does_not_take_the_server_down(self, tmp_path):
        header = "zh,en,aliases,category\n"
        csv = tmp_path / "glossary.csv"
        csv.write_text(header + "額度,credit limit,,授信\n", encoding="utf-8")

        async with mcp_session(env={"GLOSSARY_CSV": str(csv)}) as server:
            assert json.loads(await server.call("lookup_terms", {"text": "額度"}))["count"] == 1

            csv.write_text("this is not a glossary\n", encoding="utf-8")

            payload = json.loads(await server.call("lookup_terms", {"text": "額度"}))
            assert payload["count"] == 1, "a bad edit should degrade to stale, not to down"
