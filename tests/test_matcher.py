"""Unit and golden-fixture tests for `glossary.matcher` (spec 001 §6)."""

import pytest

from contracts.tools import Verdict
from glossary.loader import load_glossary
from glossary.matcher import hit_rate, match_terms, missed_terms
from glossary.scanner import scan


class TestGoldenFixtures:
    def test_all_cases(self, glossary, load_fixture):
        failures = []
        for case in load_fixture("verify_cases.json")["cases"]:
            verdicts = match_terms(case["translation"], scan(case["source"], glossary), glossary)
            got = {v.zh: v.verdict.value for v in verdicts}
            if got != case["expect"]:
                failures.append(f"{case['name']}: got {got}, want {case['expect']}")

            rate = hit_rate(verdicts)
            if abs(rate - case["hit_rate"]) > 1e-9:
                failures.append(f"{case['name']}: hit_rate {rate}, want {case['hit_rate']}")

            for zh, expected_found in case.get("found", {}).items():
                actual = next(v.found for v in verdicts if v.zh == zh)
                if actual != expected_found:
                    failures.append(
                        f"{case['name']}: found for {zh} was {actual!r}, want {expected_found!r}"
                    )
        assert not failures, "\n".join(failures)


class TestSwallowedSpans:
    """The precision rule that distinguishes a wrong term from a right one."""

    @pytest.fixture
    def overlapping(self, csv_factory):
        return load_glossary(
            csv_factory(
                [
                    ("額度", "credit limit", "", "授信"),
                    ("臨時額度", "temporary credit limit", "", "授信"),
                ]
            )
        )

    def test_short_term_inside_long_english_is_not_a_hit(self, overlapping):
        verdicts = match_terms("Insufficient temporary credit limit.", ["額度"], overlapping)
        assert verdicts[0].verdict is Verdict.WRONG
        assert verdicts[0].found == "temporary credit limit"

    def test_long_term_is_a_hit_even_though_it_contains_the_short_one(self, overlapping):
        verdicts = match_terms("The temporary credit limit applies.", ["臨時額度"], overlapping)
        assert verdicts[0].verdict is Verdict.HIT

    def test_long_term_rendered_as_short_is_wrong(self, overlapping):
        verdicts = match_terms("The credit limit applies.", ["臨時額度"], overlapping)
        assert verdicts[0].verdict is Verdict.WRONG
        assert verdicts[0].found == "credit limit"

    def test_a_separate_occurrence_rescues_the_short_term(self, overlapping):
        """`credit limit` on its own counts, even with a longer match elsewhere."""
        verdicts = match_terms(
            "The credit limit and the temporary credit limit differ.", ["額度"], overlapping
        )
        assert verdicts[0].verdict is Verdict.HIT

    def test_absent_term_is_a_miss_not_a_wrong(self, overlapping):
        verdicts = match_terms("Nothing relevant here.", ["額度"], overlapping)
        assert verdicts[0].verdict is Verdict.MISS
        assert verdicts[0].found is None


class TestMatcherBasics:
    def test_accepts_scan_output_entries_or_bare_strings(self, glossary):
        translation = "The temporary credit limit was raised."
        from_scan = match_terms(translation, scan("臨時額度", glossary), glossary)
        from_str = match_terms(translation, ["臨時額度"], glossary)
        from_entry = match_terms(translation, [glossary.by_zh["臨時額度"]], glossary)
        assert from_scan == from_str == from_entry

    def test_deduplicates_repeated_terms(self, glossary):
        verdicts = match_terms("funds transfer", ["轉帳", "轉帳"], glossary)
        assert len(verdicts) == 1

    def test_ignores_terms_not_in_the_glossary(self, glossary):
        assert match_terms("anything", ["不存在的術語"], glossary) == []

    def test_empty_term_list_scores_one(self, glossary):
        assert hit_rate(match_terms("anything", [], glossary)) == 1.0

    def test_hit_rate_is_a_proportion(self, glossary):
        verdicts = match_terms(
            "The posted interest rate of the account.",
            scan("外幣帳戶的牌告利率", glossary),
            glossary,
        )
        assert hit_rate(verdicts) == 0.5

    def test_missed_terms_lists_everything_not_hit(self, glossary):
        verdicts = match_terms(
            "The posted interest rate of the account.",
            scan("外幣帳戶的牌告利率", glossary),
            glossary,
        )
        assert missed_terms(verdicts) == ["外幣帳戶"]

    def test_acronym_only_translation_counts(self, glossary):
        verdicts = match_terms("Please complete KYC.", ["認識你的客戶"], glossary)
        assert verdicts[0].verdict is Verdict.HIT


class TestSharedWithOfflineEvaluation:
    def test_every_glossary_term_can_be_matched_from_its_own_english(self, glossary):
        """A term whose own English does not match itself is an unusable row.

        This is the check that keeps the asset honest as it grows to 379 terms:
        a stray character in `en` would otherwise show up as a permanent MISS
        that no amount of model work could fix.
        """
        broken = [
            entry.zh
            for entry in glossary.entries
            if match_terms(entry.en, [entry.zh], glossary)[0].verdict is not Verdict.HIT
        ]
        assert not broken, f"terms that cannot match their own English: {broken}"
