"""Phase 3 — validation and expansion. Acceptance criteria 10-12."""

import json

import pytest

from agent.gateway import HTTPGateway
from agent.loop import AgentLoop
from agent.mcp_client import LocalToolRunner
from agent.metrics import RunRecord, format_report, summarize
from agent.testing import RuleBasedGateway
from capabilities.translation.policy import TranslationSelfCheck
from evals.run_eval import glossary_cases, routing_cases, translation_cases
from tests.mcp_session import mcp_session

TARGET_HIT_RATE = 0.98
TARGET_ROUTING = 0.90

requires_gateway = pytest.mark.skipif(
    not HTTPGateway.configured(),
    reason="no GATEWAY_BASE_URL configured — model behaviour cannot be measured",
)


async def run_cases(gateway, tools, cases):
    records = []
    for case in cases:
        result = await AgentLoop(
            gateway, tools, self_check=TranslationSelfCheck()
        ).run(case["text"])
        records.append(RunRecord(case["text"], result, expected_tool=case.get("expected_tool")))
    return summarize(records)


class TestCriterion10GlossaryHitRate:
    """One end-to-end request per glossary term; hit rate must clear 98%.

    The suite is generated from the glossary rather than a fixture file, so
    pointing `GLOSSARY_CSV` at the production 379-term asset scales it with no
    code change — the 71 terms here are the sample that ships.
    """

    def test_the_suite_covers_every_term(self, glossary):
        cases = glossary_cases()
        assert len(cases) == len(glossary.entries)

    async def test_every_term_survives_the_full_pipeline(self, all_specs, glossary):
        """Scanner → tool → prompt → matcher, once per term.

        This is not a claim about a model: the double follows the glossary
        perfectly by construction. What it does prove is that no row in the asset
        is unusable — a term the scanner cannot find, or whose English the matcher
        cannot recognise, would be a permanent MISS no model could fix.
        """
        report = await run_cases(
            RuleBasedGateway(), LocalToolRunner(all_specs), glossary_cases()
        )

        assert report.runs == len(glossary.entries)
        assert report.terms_total >= report.runs
        assert report.glossary_hit_rate == 1.0, report.routing_errors

    @requires_gateway
    async def test_the_model_clears_the_offline_baseline(self):
        gateway = HTTPGateway.from_env()
        try:
            async with mcp_session() as server:
                report = await run_cases(gateway, server, glossary_cases())
        finally:
            await gateway.aclose()

        print(f"\n{format_report(report)}")
        assert report.glossary_hit_rate >= TARGET_HIT_RATE


class TestCriterion11RoutingAtFiveTools:
    """Tool selection stays ≥90% once the registry holds five tools."""

    def test_the_registry_really_has_five(self, all_specs):
        assert len(all_specs) == 5
        assert set(all_specs) == {
            "lookup_terms",
            "verify_translation",
            "get_time",
            "say_hello",
            "get_weather",
        }

    def test_the_tool_matrix_is_covered(self, all_specs):
        """No arguments / arguments / external dependency — the plan's matrix."""
        assert all_specs["get_time"].input_schema["required"] == []
        assert all_specs["say_hello"].input_schema["required"] == ["name"]
        assert "external" in all_specs["get_weather"].tags

    async def test_the_measurement_works_at_five_tools(self, all_specs):
        report = await run_cases(
            RuleBasedGateway(), LocalToolRunner(all_specs), routing_cases()
        )
        assert report.tool_selection_accuracy >= TARGET_ROUTING

    @requires_gateway
    async def test_the_model_routes_correctly_at_five_tools(self):
        gateway = HTTPGateway.from_env()
        try:
            async with mcp_session() as server:
                assert len(server.tool_names) == 5
                report = await run_cases(gateway, server, routing_cases())
        finally:
            await gateway.aclose()

        print(f"\n{format_report(report)}")
        assert report.tool_selection_accuracy >= TARGET_ROUTING, report.routing_errors


class TestCriterion12ToolCallRateIsReported:
    """The report must carry the tool-call rate — the plan's headline metric."""

    async def test_it_appears_in_the_dict_and_the_text(self, all_specs):
        report = await run_cases(
            RuleBasedGateway(), LocalToolRunner(all_specs), routing_cases()
        )
        assert "tool_call_rate" in report.to_dict()
        assert "tool call rate" in format_report(report)

    async def test_it_counts_runs_where_the_model_skipped_the_tools(self, all_specs):
        runner = LocalToolRunner(all_specs)
        cases = routing_cases()[:4]

        used = [
            RunRecord(c["text"], await AgentLoop(RuleBasedGateway(), runner).run(c["text"]))
            for c in cases[:3]
        ]
        skipped = [
            RunRecord(
                cases[3]["text"],
                await AgentLoop(RuleBasedGateway(use_tools=False), runner).run(cases[3]["text"]),
            )
        ]

        report = summarize(used + skipped)
        assert report.tool_call_rate == 0.75

    def test_the_eval_harness_writes_the_metric_to_its_report(self, tmp_path):
        """Criterion 12 is about the *report*, so check the artefact on disk.

        Synchronous on purpose: `main()` owns its own event loop, exactly as it
        does on the command line.
        """
        from evals.run_eval import main

        destination = tmp_path / "report.json"
        exit_code = main(
            ["--suite", "routing", "--gateway", "fake", "--limit", "4", "--out", str(destination)]
        )
        assert exit_code == 0

        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert payload["gateway"] == "fake"
        assert payload["suites"]["routing"]["tool_call_rate"] == 1.0
        assert "tool_selection_accuracy" in payload["suites"]["routing"]
        assert payload["glossary_terms"] > 0

    def test_all_three_eval_suites_build(self, glossary):
        assert len(routing_cases()) == 10
        assert len(translation_cases()) == 20
        assert len(glossary_cases()) == len(glossary.entries)
