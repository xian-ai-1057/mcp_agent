"""Unit tests for the tool layer (spec 002 §2, §5)."""

import json

import pytest

from tools.base import ToolError, ToolSpec, object_schema
from tools.registry import discover


class TestToolSpec:
    def _spec(self, **overrides):
        defaults = dict(
            name="ok_tool",
            description="does a thing",
            input_schema=object_schema({}),
            handler=lambda args: {},
        )
        defaults.update(overrides)
        return ToolSpec(**defaults)

    def test_valid_spec(self):
        assert self._spec().name == "ok_tool"

    @pytest.mark.parametrize("name", ["Bad", "1bad", "bad-name", "", "bad name"])
    def test_rejects_bad_names(self, name):
        with pytest.raises(ValueError, match="tool name"):
            self._spec(name=name)

    def test_rejects_empty_description(self):
        with pytest.raises(ValueError, match="empty description"):
            self._spec(description="   ")

    def test_rejects_non_object_schema(self):
        with pytest.raises(ValueError, match="type 'object'"):
            self._spec(input_schema={"type": "string"})

    def test_run_tolerates_none_arguments(self):
        spec = self._spec(handler=lambda args: {"got": args})
        assert spec.run(None) == {"got": {}}


class TestObjectSchema:
    def test_is_strict_by_default(self):
        schema = object_schema({"a": {"type": "string"}}, required=["a"])
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["a"]


class TestRegistry:
    def test_discovers_all_five_tools(self, all_specs):
        assert set(all_specs) == {
            "lookup_terms",
            "verify_translation",
            "get_time",
            "say_hello",
            "get_weather",
        }

    def test_every_spec_is_named_after_its_key(self, all_specs):
        for name, spec in all_specs.items():
            assert spec.name == name

    def test_every_description_says_when_to_call(self, all_specs):
        """Descriptions carry the whole routing burden (spec 002 §4)."""
        for name, spec in all_specs.items():
            assert "call" in spec.description.lower(), f"{name} never says when to call it"

    def test_every_schema_is_strict_and_documented(self, all_specs):
        for name, spec in all_specs.items():
            schema = spec.input_schema
            assert schema.get("additionalProperties") is False, name
            for prop, definition in schema.get("properties", {}).items():
                assert definition.get("description"), f"{name}.{prop} has no description"

    def test_registry_is_a_fresh_dict_each_call(self):
        assert discover() is not discover()


class TestLookupTerms:
    def test_returns_matches_block_and_count(self, all_specs):
        result = all_specs["lookup_terms"].run({"text": "客戶申請提高臨時額度"})
        assert result["count"] == 1
        assert result["matches"][0]["zh"] == "臨時額度"
        assert result["glossary_block"] == "- 臨時額度 → temporary credit limit"

    def test_block_deduplicates_but_matches_do_not(self, all_specs):
        result = all_specs["lookup_terms"].run({"text": "轉帳與轉帳手續費"})
        assert result["count"] == 2
        assert result["glossary_block"].count("轉帳") == 1

    def test_no_terms_gives_an_empty_block(self, all_specs):
        result = all_specs["lookup_terms"].run({"text": "今天心情很好"})
        assert result == {"matches": [], "glossary_block": "", "count": 0}

    def test_result_is_json_serialisable(self, all_specs):
        json.dumps(all_specs["lookup_terms"].run({"text": "臨時額度"}))


class TestVerifyTranslation:
    def test_all_terms_used(self, all_specs):
        result = all_specs["verify_translation"].run(
            {
                "source_text": "客戶申請提高臨時額度",
                "translation": "The customer applied to raise the temporary credit limit.",
            }
        )
        assert result["hit_rate"] == 1.0
        assert result["missed"] == []
        assert result["results"][0]["verdict"] == "HIT"

    def test_wrong_term_is_reported_with_what_was_written(self, all_specs):
        result = all_specs["verify_translation"].run(
            {
                "source_text": "客戶申請提高臨時額度",
                "translation": "The customer applied to raise the credit limit.",
            }
        )
        assert result["hit_rate"] == 0.0
        assert result["missed"] == ["臨時額度"]
        assert result["results"][0]["found"] == "credit limit"

    def test_a_source_with_no_terms_cannot_fail(self, all_specs):
        result = all_specs["verify_translation"].run(
            {"source_text": "今天心情很好", "translation": "I feel great today."}
        )
        assert result["hit_rate"] == 1.0

    def test_is_stateless(self, all_specs):
        """Two identical calls give identical results; nothing is remembered."""
        args = {"source_text": "臨時額度", "translation": "credit limit"}
        assert all_specs["verify_translation"].run(args) == all_specs["verify_translation"].run(args)


class TestGetTime:
    def test_defaults_to_taipei(self, all_specs):
        assert all_specs["get_time"].run({})["timezone"] == "Asia/Taipei"

    def test_honours_an_explicit_zone(self, all_specs):
        result = all_specs["get_time"].run({"timezone": "America/New_York"})
        assert result["timezone"] == "America/New_York"
        assert result["utc_offset"] in {"-0400", "-0500"}

    def test_unknown_zone_is_a_tool_error(self, all_specs):
        with pytest.raises(ToolError, match="unknown timezone"):
            all_specs["get_time"].run({"timezone": "Mars/Olympus"})

    def test_shape(self, all_specs):
        result = all_specs["get_time"].run({})
        assert set(result) == {"iso", "date", "time", "timezone", "weekday", "utc_offset"}


class TestSayHello:
    def test_chinese_by_default(self, all_specs):
        result = all_specs["say_hello"].run({"name": "王小明"})
        assert result["language"] == "zh"
        assert "王小明" in result["greeting"]

    def test_english(self, all_specs):
        result = all_specs["say_hello"].run({"name": "Alex", "language": "en"})
        assert result["greeting"].startswith("Hello, Alex")

    @pytest.mark.parametrize("arguments", [{"name": ""}, {"name": "   "}])
    def test_empty_name_is_a_tool_error(self, all_specs, arguments):
        with pytest.raises(ToolError, match="name must not be empty"):
            all_specs["say_hello"].run(arguments)

    def test_unsupported_language_is_a_tool_error(self, all_specs):
        with pytest.raises(ToolError, match="unsupported language"):
            all_specs["say_hello"].run({"name": "Alex", "language": "fr"})


class TestGetWeather:
    def test_defaults_to_the_offline_stub(self, all_specs, monkeypatch):
        """No network by default — the data source is still an open question."""
        monkeypatch.delenv("WEATHER_PROVIDER", raising=False)
        result = all_specs["get_weather"].run({"city": "台北"})
        assert result["source"] == "stub"
        assert isinstance(result["temperature_c"], float)

    def test_unknown_city_still_answers_from_the_stub(self, all_specs, monkeypatch):
        monkeypatch.setenv("WEATHER_PROVIDER", "stub")
        assert all_specs["get_weather"].run({"city": "無此城市"})["temperature_c"] == 25.0

    def test_empty_city_is_a_tool_error(self, all_specs):
        with pytest.raises(ToolError, match="city must not be empty"):
            all_specs["get_weather"].run({"city": "  "})

    def test_unknown_provider_is_a_tool_error(self, all_specs, monkeypatch):
        monkeypatch.setenv("WEATHER_PROVIDER", "acme-weather")
        with pytest.raises(ToolError, match="unknown WEATHER_PROVIDER"):
            all_specs["get_weather"].run({"city": "台北"})

    def test_provider_registry_lists_both_options(self):
        from tools.get_weather import PROVIDERS

        assert set(PROVIDERS) == {"stub", "open-meteo"}


class TestGlossaryToolsSeeReloads:
    def test_lookup_reflects_an_edited_csv_without_a_restart(self, all_specs, installed_glossary):
        path, loader = installed_glossary([("額度", "credit limit", "", "授信")])
        assert all_specs["lookup_terms"].run({"text": "臨時額度"})["count"] == 1

        path.write_text(
            "zh,en,aliases,category\n額度,credit limit,,授信\n臨時額度,temporary credit limit,,授信\n",
            encoding="utf-8",
        )
        result = all_specs["lookup_terms"].run({"text": "臨時額度"})
        assert result["matches"][0]["zh"] == "臨時額度"
        assert loader.reload_count == 2

    def test_lookup_uses_unrelated_rows_when_duplicates_are_quarantined(
        self, all_specs, installed_glossary
    ):
        installed_glossary(
            [
                ("信用卡", "Credit Card", "", "卡片"),
                ("警示帳戶", "Watchlisted Account", "", "風控"),
                ("警示帳戶", "Warning Account", "", "警示帳戶"),
            ],
            conflict_policy="quarantine",
        )

        result = all_specs["lookup_terms"].run({"text": "客戶申請信用卡"})

        assert result["matches"] == [
            {"zh": "信用卡", "en": "Credit Card", "start": 4, "end": 7}
        ]

    def test_lookup_reports_a_quarantined_term_as_a_tool_error(
        self, all_specs, installed_glossary
    ):
        installed_glossary(
            [
                ("警示帳戶", "Watchlisted Account", "", "風控"),
                ("警示帳戶", "Warning Account", "", "警示帳戶"),
            ],
            conflict_policy="quarantine",
        )

        with pytest.raises(ToolError, match=r"conflicting translations on lines 2, 3"):
            all_specs["lookup_terms"].run({"text": "這是警示帳戶"})
