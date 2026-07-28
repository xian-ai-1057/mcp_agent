"""Unit tests for `glossary.loader` (spec 001 §4)."""

import os
import threading

import pytest

from contracts.glossary import GlossaryEntry
from glossary.loader import (
    GlossaryConflictError,
    GlossaryError,
    GlossaryLoader,
    build_glossary,
    load_glossary,
)
from glossary.scanner import scan

BASIC = [
    ("額度", "credit limit", "", "授信"),
    ("臨時額度", "temporary credit limit", "", "授信"),
    ("洗錢防制", "Anti-Money Laundering (AML)", "防制洗錢", "法遵"),
]


def touch_newer(path):
    """Guarantee the change is visible even on a coarse-grained filesystem."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class TestLoad:
    def test_reads_entries_and_aliases(self, csv_factory):
        glossary = load_glossary(csv_factory(BASIC))
        assert len(glossary) == 3
        assert glossary.by_zh["額度"].en == "credit limit"
        assert glossary.by_zh["洗錢防制"].aliases == ["防制洗錢"]
        assert glossary.surface_to_entry["防制洗錢"].zh == "洗錢防制"

    def test_scan_pattern_is_ordered_longest_first(self, csv_factory):
        glossary = load_glossary(csv_factory(BASIC))
        surfaces = glossary.scan_pattern.pattern.split("|")
        lengths = [len(s) for s in surfaces]
        assert lengths == sorted(lengths, reverse=True)

    def test_records_overlapping_terms_in_both_directions(self, csv_factory):
        glossary = load_glossary(csv_factory(BASIC))
        assert {e.zh for e in glossary.overlapping("額度")} == {"臨時額度"}
        assert {e.zh for e in glossary.overlapping("臨時額度")} == {"額度"}
        assert glossary.overlapping("洗錢防制") == ()

    def test_tolerates_blank_lines(self, tmp_path):
        path = tmp_path / "g.csv"
        path.write_text("zh,en,aliases,category\n\n額度,credit limit,,授信\n", encoding="utf-8")
        assert len(load_glossary(path)) == 1

    def test_tolerates_bom(self, tmp_path):
        path = tmp_path / "g.csv"
        path.write_text(
            "﻿zh,en,aliases,category\n額度,credit limit,,授信\n", encoding="utf-8"
        )
        assert len(load_glossary(path)) == 1


class TestLoadErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(GlossaryError, match="not found"):
            load_glossary(tmp_path / "nope.csv")

    def test_missing_column(self, tmp_path):
        path = tmp_path / "g.csv"
        path.write_text("zh,en\n額度,credit limit\n", encoding="utf-8")
        with pytest.raises(GlossaryError, match="missing required column"):
            load_glossary(path)

    def test_duplicate_term_names_both_lines(self, csv_factory):
        path = csv_factory([("額度", "credit limit", "", "授信"), ("額度", "quota", "", "授信")])
        with pytest.raises(GlossaryError, match="duplicate term"):
            load_glossary(path)

    def test_blank_required_field(self, csv_factory):
        with pytest.raises(GlossaryError, match="invalid row"):
            load_glossary(csv_factory([("額度", "", "", "授信")]))

    def test_alias_claimed_by_two_entries(self, csv_factory):
        path = csv_factory(
            [("額度", "credit limit", "共用", "授信"), ("帳戶", "account", "共用", "帳戶")]
        )
        with pytest.raises(
            GlossaryError,
            match=r":3: alias '共用'.*first claimed on line 2",
        ):
            load_glossary(path)

    def test_alias_colliding_with_a_canonical_term_keeps_the_canonical(self, csv_factory):
        path = csv_factory(
            [("額度", "credit limit", "", "授信"), ("臨時額度", "temporary credit limit", "額度", "授信")]
        )
        glossary = load_glossary(path)
        assert glossary.surface_to_entry["額度"].zh == "額度"

    def test_empty_file(self, tmp_path):
        path = tmp_path / "g.csv"
        path.write_text("zh,en,aliases,category\n", encoding="utf-8")
        with pytest.raises(GlossaryError, match="no usable rows"):
            load_glossary(path)

    def test_unknown_conflict_policy(self, csv_factory):
        with pytest.raises(ValueError, match="unknown glossary conflict policy"):
            load_glossary(csv_factory(BASIC), conflict_policy="guess")

    def test_low_level_builder_never_silently_resolves_duplicate_entries(self, tmp_path):
        entries = (
            GlossaryEntry(zh="警示帳戶", en="Watchlisted Account", category="風控"),
            GlossaryEntry(zh="警示帳戶", en="Warning Account", category="警示帳戶"),
        )

        with pytest.raises(GlossaryError, match="duplicate term"):
            build_glossary(entries, tmp_path / "g.csv", conflict_policy="quarantine")


class TestQuarantine:
    def test_conflicting_duplicate_does_not_block_unrelated_terms(self, csv_factory):
        path = csv_factory(
            [
                ("信用卡", "Credit Card", "", "卡片"),
                ("警示帳戶", "Watchlisted Account", "", "風控"),
                ("警示帳戶", "Warning Account", "", "警示帳戶"),
            ]
        )

        glossary = load_glossary(path, conflict_policy="quarantine")

        assert [match.en for match in scan("客戶申請信用卡", glossary)] == ["Credit Card"]
        assert "警示帳戶" not in glossary.by_zh
        assert glossary.conflicts["警示帳戶"].lines == (3, 4)

    def test_conflicting_term_fails_instead_of_choosing_first_or_last(self, csv_factory):
        path = csv_factory(
            [
                ("帳戶", "Account", "", "帳戶"),
                ("警示帳戶", "Watchlisted Account", "", "風控"),
                ("警示帳戶", "Warning Account", "", "警示帳戶"),
            ]
        )
        glossary = load_glossary(path, conflict_policy="quarantine")

        with pytest.raises(GlossaryConflictError, match=r"lines 3, 4"):
            scan("這是警示帳戶", glossary)

    def test_identical_translations_are_collapsed_and_aliases_merged(self, csv_factory):
        path = csv_factory(
            [
                ("交易監控", "Transaction Monitoring", "交易監測", "風控"),
                ("交易監控", "transaction-monitoring", "監控交易", "ATM"),
            ]
        )

        glossary = load_glossary(path, conflict_policy="quarantine")

        assert len(glossary) == 1
        assert glossary.by_zh["交易監控"].aliases == ["交易監測", "監控交易"]
        assert glossary.conflicts == {}

    def test_shared_alias_is_quarantined_but_canonical_terms_remain(self, csv_factory):
        path = csv_factory(
            [
                ("額度", "credit limit", "共同別名", "授信"),
                ("帳戶", "account", "共同別名", "帳戶"),
                ("貸款", "loan", "共同別名", "貸款"),
            ]
        )
        glossary = load_glossary(path, conflict_policy="quarantine")

        assert [match.zh for match in scan("額度、帳戶與貸款", glossary)] == [
            "額度",
            "帳戶",
            "貸款",
        ]
        with pytest.raises(GlossaryConflictError, match=r"lines 2, 3, 4"):
            scan("共同別名", glossary)


class TestReload:
    def test_unchanged_file_is_not_reloaded(self, csv_factory):
        loader = GlossaryLoader(csv_factory(BASIC))
        loader.get()
        loader.get()
        loader.get()
        assert loader.reload_count == 1

    def test_edited_file_is_reloaded_without_restart(self, csv_factory):
        path = csv_factory(BASIC)
        loader = GlossaryLoader(path)
        assert "新術語" not in loader.get().by_zh

        path.write_text(
            path.read_text(encoding="utf-8") + "新術語,brand new term,,授信\n", encoding="utf-8"
        )
        touch_newer(path)

        assert loader.get().by_zh["新術語"].en == "brand new term"
        assert loader.reload_count == 2

    def test_same_size_edit_is_still_detected(self, csv_factory):
        """mtime carries the change when the byte count happens to match."""
        path = csv_factory(BASIC)
        loader = GlossaryLoader(path)
        loader.get()

        path.write_text(
            path.read_text(encoding="utf-8").replace("credit limit", "credit limix"),
            encoding="utf-8",
        )
        touch_newer(path)

        assert loader.get().by_zh["額度"].en == "credit limix"

    def test_broken_edit_degrades_to_stale_not_down(self, csv_factory):
        path = csv_factory(BASIC)
        loader = GlossaryLoader(path)
        loader.get()

        path.write_text("this is not a glossary at all\n", encoding="utf-8")
        touch_newer(path)

        glossary = loader.get()
        assert glossary.by_zh["額度"].en == "credit limit"
        assert loader.reload_count == 1

    def test_first_load_of_a_broken_file_still_raises(self, tmp_path):
        path = tmp_path / "broken.csv"
        path.write_text("nonsense\n", encoding="utf-8")
        with pytest.raises(GlossaryError):
            GlossaryLoader(path).get()

    def test_deleted_file_serves_the_cached_copy(self, csv_factory):
        path = csv_factory(BASIC)
        loader = GlossaryLoader(path)
        loader.get()
        path.unlink()
        assert loader.get().by_zh["額度"].en == "credit limit"

    def test_concurrent_readers_never_see_a_half_built_glossary(self, csv_factory):
        path = csv_factory(BASIC)
        loader = GlossaryLoader(path)
        seen: list[int] = []
        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(20):
                    seen.append(len(loader.get()))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert set(seen) == {3}

    def test_quarantine_reload_adopts_usable_rows_and_isolates_conflict(self, csv_factory):
        path = csv_factory([("警示帳戶", "alert account", "", "帳戶")])
        loader = GlossaryLoader(path, conflict_policy="quarantine")
        assert loader.get().by_zh["警示帳戶"].en == "alert account"

        path.write_text(
            "zh,en,aliases,category\n"
            "警示帳戶,Watchlisted Account,,風控\n"
            "警示帳戶,Warning Account,,警示帳戶\n"
            "信用卡,Credit Card,,卡片\n",
            encoding="utf-8",
        )
        touch_newer(path)

        glossary = loader.get()
        assert glossary.by_zh["信用卡"].en == "Credit Card"
        assert "警示帳戶" not in glossary.by_zh
        assert loader.reload_count == 2
