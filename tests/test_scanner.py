"""Unit and golden-fixture tests for `glossary.scanner` (spec 001 §5)."""

import time

import pytest

from glossary.loader import load_glossary
from glossary.scanner import scan, unique_terms


def _case_ids(cases):
    return [case["name"] for case in cases]


class TestGoldenFixtures:
    """Every case in `fixtures/scan_cases.json`, matched exactly."""

    def test_all_cases(self, glossary, load_fixture):
        failures = []
        for case in load_fixture("scan_cases.json")["cases"]:
            got = [(m.zh, m.en, m.start, m.end) for m in scan(case["text"], glossary)]
            want = [(e["zh"], e["en"], e["start"], e["end"]) for e in case["expect"]]
            if got != want:
                failures.append(f"{case['name']}: got {got}, want {want}")

            present = {m[0] for m in got}
            leaked = [zh for zh in case["forbidden"] if zh in present]
            if leaked:
                failures.append(f"{case['name']}: forbidden terms present: {leaked}")
        assert not failures, "\n".join(failures)

    def test_spans_point_at_the_text_that_was_actually_written(self, glossary, load_fixture):
        for case in load_fixture("scan_cases.json")["cases"]:
            for match in scan(case["text"], glossary):
                surface = case["text"][match.start : match.end]
                entry = glossary.by_zh[match.zh]
                assert surface in entry.surfaces, f"{surface!r} is not a surface of {match.zh}"


class TestLongestMatchFirst:
    @pytest.fixture
    def overlapping(self, csv_factory):
        return load_glossary(
            csv_factory(
                [
                    ("額度", "credit limit", "", "授信"),
                    ("臨時額度", "temporary credit limit", "", "授信"),
                    ("永久額度", "permanent credit limit", "", "授信"),
                    ("帳戶", "account", "", "帳戶"),
                    ("警示帳戶", "alert account", "", "帳戶"),
                    ("警示帳戶通報機制", "alert account reporting mechanism", "", "帳戶"),
                ]
            )
        )

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("臨時額度", ["臨時額度"]),
            ("提高臨時額度", ["臨時額度"]),
            ("永久額度和臨時額度", ["永久額度", "臨時額度"]),
            ("額度", ["額度"]),
            ("額度與臨時額度", ["額度", "臨時額度"]),
            ("警示帳戶通報機制", ["警示帳戶通報機制"]),
            ("警示帳戶", ["警示帳戶"]),
            ("帳戶", ["帳戶"]),
            ("警示帳戶與帳戶", ["警示帳戶", "帳戶"]),
        ],
    )
    def test_nesting(self, overlapping, text, expected):
        assert [m.zh for m in scan(text, overlapping)] == expected

    def test_a_covered_short_term_is_never_injected(self, overlapping):
        """The failure this rule exists to prevent.

        `額度` and `臨時額度` have contradictory English. Emitting both would hand
        the model two different instructions about the same characters.
        """
        matches = scan("客戶申請提高臨時額度", overlapping)
        assert [m.en for m in matches] == ["temporary credit limit"]

    def test_valid_longer_term_suppresses_nested_quarantined_term(self, csv_factory):
        glossary = load_glossary(
            csv_factory(
                [
                    ("警示帳戶", "Watchlisted Account", "", "風控"),
                    ("警示帳戶", "Warning Account", "", "警示帳戶"),
                    ("警示帳戶通報機制", "alert account reporting mechanism", "", "帳戶"),
                ]
            ),
            conflict_policy="quarantine",
        )

        assert [match.zh for match in scan("警示帳戶通報機制", glossary)] == [
            "警示帳戶通報機制"
        ]


class TestScanBasics:
    def test_empty_text(self, glossary):
        assert scan("", glossary) == []

    def test_text_with_no_terms(self, glossary):
        assert scan("今天心情很好", glossary) == []

    def test_matches_are_in_text_order(self, glossary):
        matches = scan("外幣帳戶的牌告利率與轉帳", glossary)
        starts = [m.start for m in matches]
        assert starts == sorted(starts)

    def test_repeats_are_each_returned(self, glossary):
        matches = scan("轉帳與轉帳手續費", glossary)
        assert len(matches) == 2
        assert matches[0].start != matches[1].start

    def test_unique_terms_keeps_first_occurrence(self, glossary):
        matches = scan("轉帳與轉帳手續費", glossary)
        unique = unique_terms(matches)
        assert len(unique) == 1
        assert unique[0].start == matches[0].start


class TestPerformance:
    def test_scan_throughput(self, glossary):
        """Record the cost. Optimise only if this becomes a problem (spec §7)."""
        text = "客戶申請提高臨時額度，並查詢外幣帳戶的牌告利率與交易明細。" * 10
        started = time.perf_counter()
        for _ in range(200):
            scan(text, glossary)
        elapsed = time.perf_counter() - started
        per_scan_ms = elapsed / 200 * 1000
        print(f"\n{len(glossary)} terms, {len(text)} chars: {per_scan_ms:.3f} ms/scan")
        # A very loose ceiling: this asserts "not pathological", not a target.
        assert per_scan_ms < 50
