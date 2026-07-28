"""Unit tests for `glossary.loader` (spec 001 §4)."""

import os
import threading

import pytest

from glossary.loader import GlossaryError, GlossaryLoader, load_glossary

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
        with pytest.raises(GlossaryError, match="claimed by both"):
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
