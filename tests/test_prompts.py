"""Unit tests for `agent.prompts` (spec 003 §3)."""

import ast
from pathlib import Path

from agent.prompts import (
    SYSTEM_PROMPT,
    format_glossary_block,
    glossary_prompt,
    retranslate_prompt,
)
from contracts.glossary import TermMatch
from contracts.tools import TermVerdict, Verdict, VerifyResult
from tools.registry import discover


class TestSystemPrompt:
    def test_names_no_tool(self):
        """The prompt must stay generic — this is what makes criterion 6 hold.

        A tool list in prose is a second copy of the registry that goes stale the
        moment a tool is added.
        """
        for name in discover():
            assert name not in SYSTEM_PROMPT, f"system prompt mentions the tool {name!r}"

    def test_states_the_glossary_ordering_rule(self):
        assert "先查詢術語" in SYSTEM_PROMPT

    def test_is_not_empty(self):
        assert len(SYSTEM_PROMPT.strip()) > 50


class TestPromptsModuleIsPure:
    def test_imports_nothing_but_contracts(self):
        """`tools/` imports this module; it must not drag in the client layer.

        Enforced by reading the imports rather than by convention, because the
        cost of getting it wrong is a circular dependency that only shows up when
        the MCP server starts.
        """
        source = Path("agent/prompts.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
        assert modules <= {"contracts"}, f"prompts.py imports {modules - {'contracts'}}"


class TestFormatGlossaryBlock:
    def _match(self, zh, en, start=0):
        return TermMatch(zh=zh, en=en, start=start, end=start + len(zh))

    def test_one_line_per_term(self):
        block = format_glossary_block(
            [self._match("臨時額度", "temporary credit limit"), self._match("轉帳", "funds transfer", 10)]
        )
        assert block == "- 臨時額度 → temporary credit limit\n- 轉帳 → funds transfer"

    def test_deduplicates_keeping_first_occurrence_order(self):
        block = format_glossary_block(
            [
                self._match("轉帳", "funds transfer", 0),
                self._match("額度", "credit limit", 5),
                self._match("轉帳", "funds transfer", 20),
            ]
        )
        assert block.splitlines() == ["- 轉帳 → funds transfer", "- 額度 → credit limit"]

    def test_no_matches_gives_an_empty_string(self):
        assert format_glossary_block([]) == ""


class TestGlossaryPrompt:
    def test_includes_the_block_and_the_text(self):
        prompt = glossary_prompt("客戶申請提高臨時額度", "- 臨時額度 → temporary credit limit")
        assert "temporary credit limit" in prompt
        assert "客戶申請提高臨時額度" in prompt

    def test_without_a_block_it_is_just_the_instruction(self):
        prompt = glossary_prompt("今天心情很好", "")
        assert "術語" not in prompt
        assert "今天心情很好" in prompt


class TestRetranslatePrompt:
    def _verify(self):
        return VerifyResult(
            results=[
                TermVerdict(
                    zh="臨時額度",
                    expected_en="temporary credit limit",
                    verdict=Verdict.WRONG,
                    found="credit limit",
                ),
                TermVerdict(zh="轉帳", expected_en="funds transfer", verdict=Verdict.MISS),
                TermVerdict(zh="額度", expected_en="credit limit", verdict=Verdict.HIT),
            ],
            hit_rate=1 / 3,
            missed=["臨時額度", "轉帳"],
        )

    def test_names_every_missed_term_with_its_required_english(self):
        prompt = retranslate_prompt("原文", "previous attempt", self._verify())
        assert "臨時額度 必須譯為 temporary credit limit" in prompt
        assert "轉帳 必須譯為 funds transfer" in prompt

    def test_says_what_was_written_instead(self):
        assert "你寫的是「credit limit」" in retranslate_prompt("原文", "prev", self._verify())

    def test_leaves_correct_terms_alone(self):
        prompt = retranslate_prompt("原文", "prev", self._verify())
        # Line-level, because "額度 必須譯為" is a substring of the 臨時額度 line.
        correction_lines = [line for line in prompt.splitlines() if "必須譯為" in line]
        assert not any(line.startswith("- 額度 ") for line in correction_lines)
        assert len(correction_lines) == 2

    def test_includes_the_previous_attempt_so_the_model_repairs(self):
        assert "previous attempt" in retranslate_prompt("原文", "previous attempt", self._verify())
