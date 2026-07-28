"""Unit tests for `glossary.normalize` (spec 001 §3)."""

import pytest

from glossary.normalize import (
    compile_pattern,
    expand_forms,
    find_form,
    normalize_en,
    normalize_zh,
)


class TestNormalizeEn:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Credit Limit", "credit limit"),
            ("Anti-Money Laundering", "anti money laundering"),
            ("anti—money  laundering", "anti money laundering"),
            ("  spaced   out  ", "spaced out"),
            ("e_statement", "e statement"),
            ("ＦＵＬＬＷＩＤＴＨ", "fullwidth"),
        ],
    )
    def test_folds_case_hyphens_and_whitespace(self, text, expected):
        assert normalize_en(text) == expected

    def test_is_idempotent(self):
        once = normalize_en("Anti-Money  Laundering")
        assert normalize_en(once) == once


class TestNormalizeZh:
    def test_preserves_length_so_offsets_stay_valid(self):
        text = "客戶申請提高臨時額度"
        assert len(normalize_zh(text)) == len(text)

    def test_folds_fullwidth_digits(self):
        assert normalize_zh("１２３") == "123"


class TestExpandForms:
    def test_plain_phrase_gets_a_plural(self):
        assert expand_forms("credit limit") == ["credit limits", "credit limit"]

    def test_parenthetical_acronym_yields_three_families(self):
        forms = expand_forms("Know Your Customer (KYC)")
        assert "know your customer" in forms
        assert "kyc" in forms
        assert "know your customer (kyc)" in forms

    @pytest.mark.parametrize(
        "phrase, plural",
        [
            ("stress test", "stress tests"),
            ("facility", "facilities"),
            ("branch", "branches"),
            ("tax", "taxes"),
            ("business", "businesses"),
            ("holiday", "holidays"),
        ],
    )
    def test_plural_rules(self, phrase, plural):
        assert plural in expand_forms(phrase)

    def test_longest_first(self):
        forms = expand_forms("Anti-Money Laundering (AML)")
        assert forms == sorted(forms, key=lambda f: (-len(f), f))

    def test_drops_one_character_noise(self):
        assert "a" not in expand_forms("Thing (A)")

    def test_empty_input(self):
        assert expand_forms("   ") == []


class TestCompilePattern:
    def test_does_not_match_inside_a_longer_word(self):
        pattern = compile_pattern(expand_forms("credit limit"))
        assert find_form("there are limitations here", pattern) is None
        assert find_form("The credit limit applies", pattern) == "credit limit"

    def test_matches_next_to_punctuation(self):
        pattern = compile_pattern(expand_forms("Know Your Customer (KYC)"))
        assert find_form("complete (KYC) first", pattern) == "kyc"
        assert find_form("complete KYC.", pattern) == "kyc"

    def test_is_case_insensitive(self):
        pattern = compile_pattern(expand_forms("credit limit"))
        assert find_form("CREDIT LIMIT", pattern) == "credit limit"

    def test_empty_form_list_never_matches(self):
        assert compile_pattern([]).search("anything at all") is None
