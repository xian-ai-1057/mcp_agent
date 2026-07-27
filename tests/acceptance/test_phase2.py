"""Phase 2 — self-check closed loop. Acceptance criteria 8-9."""

import asyncio

import pytest

from agent.loop import AgentLoop, TranslationSelfCheck
from agent.mcp_client import LocalToolRunner
from agent.testing import RuleBasedGateway
from contracts.tools import Verdict
from glossary.matcher import match_terms
from glossary.scanner import scan

# Low enough to drop a term from every multi-term sentence, high enough that the
# double still behaves like a model that mostly follows the glossary.
PARTIAL_FIDELITY = 0.5


def measure(output: str, source: str, glossary) -> tuple[int, int]:
    """(hits, total) for one translation, judged by the shared matcher."""
    verdicts = match_terms(output, scan(source, glossary), glossary)
    return sum(1 for v in verdicts if v.verdict is Verdict.HIT), len(verdicts)


class TestCriterion8RetranslationRaisesHitRate:
    """Over 20 fixture sentences, the repaired hit rate beats the unrepaired one."""

    @pytest.fixture
    def cases(self, load_fixture):
        cases = load_fixture("retranslate_cases.json")["cases"]
        assert len(cases) == 20
        return cases

    def test_the_fixtures_still_describe_the_glossary(self, cases, glossary):
        """Pins the fixture set against an accidental glossary edit."""
        for case in cases:
            found = {match.zh for match in scan(case["text"], glossary)}
            assert found == set(case["expect_terms"]), case["text"]

    async def test_hit_rate_improves(self, cases, all_specs, glossary):
        runner = LocalToolRunner(all_specs)

        before_hits = before_total = 0
        for case in cases:
            loop = AgentLoop(RuleBasedGateway(glossary_fidelity=PARTIAL_FIDELITY), runner, self_check=None)
            result = await loop.run(case["text"])
            hits, total = measure(result.output, case["text"], glossary)
            before_hits += hits
            before_total += total

        after_hits = after_total = 0
        repairs = 0
        for case in cases:
            loop = AgentLoop(
                RuleBasedGateway(glossary_fidelity=PARTIAL_FIDELITY, repair_fidelity=1.0), runner
            )
            result = await loop.run(case["text"])
            hits, total = measure(result.output, case["text"], glossary)
            after_hits += hits
            after_total += total
            repairs += result.metrics.retranslations

        before_rate = before_hits / before_total
        after_rate = after_hits / after_total
        print(f"\nhit rate before {before_rate:.1%} -> after {after_rate:.1%} ({repairs} repairs)")

        assert before_total == after_total
        assert before_rate < 1.0, "the fixture must actually fail before repair, or this proves nothing"
        assert after_rate > before_rate
        assert repairs > 0

    async def test_a_clean_translation_is_never_retranslated(self, cases, all_specs):
        """Repair must be driven by the verdict, not run unconditionally."""
        runner = LocalToolRunner(all_specs)
        for case in cases[:5]:
            result = await AgentLoop(RuleBasedGateway(), runner).run(case["text"])
            assert result.verify.hit_rate == 1.0
            assert result.metrics.retranslations == 0


class TestCriterion9RetranslationCap:
    """The cap holds against a model that never improves — no infinite loop."""

    @pytest.mark.parametrize("cap", [1, 2, 3])
    async def test_the_cap_is_exact(self, all_specs, cap):
        gateway = RuleBasedGateway(glossary_fidelity=0.0, repair_fidelity=0.0)
        loop = AgentLoop(
            gateway,
            LocalToolRunner(all_specs),
            self_check=TranslationSelfCheck(max_retranslate=cap),
        )
        result = await asyncio.wait_for(loop.run("請翻譯：外幣帳戶的牌告利率"), timeout=15)

        assert result.metrics.retranslations == cap
        assert result.verify.hit_rate < 1.0

    async def test_it_terminates_rather_than_hanging(self, all_specs):
        gateway = RuleBasedGateway(glossary_fidelity=0.0, repair_fidelity=0.0)
        loop = AgentLoop(gateway, LocalToolRunner(all_specs))
        # If the cap were missing this never returns; the timeout is the assertion.
        result = await asyncio.wait_for(loop.run("請翻譯：臨時額度"), timeout=15)
        assert result.metrics.retranslations == TranslationSelfCheck().max_retranslate

    async def test_an_empty_repair_stops_the_loop_early(self, all_specs, monkeypatch):
        gateway = RuleBasedGateway(glossary_fidelity=0.0, repair_fidelity=1.0)
        monkeypatch.setattr(gateway, "_repair", lambda prompt: "")

        loop = AgentLoop(gateway, LocalToolRunner(all_specs))
        result = await asyncio.wait_for(loop.run("請翻譯：臨時額度"), timeout=15)
        assert result.metrics.retranslations == 0
        assert result.output
