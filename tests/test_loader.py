"""Alpha input loading from txt / csv / json."""

from __future__ import annotations

import json

import pytest

from worldquant.exceptions import ConfigError
from worldquant.loader import load_alphas, specs_from_expressions

DEFAULTS = {"region": "USA", "delay": 1}


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestTextLoading:
    def test_one_expression_per_line(self, tmp_path):
        path = write(tmp_path, "a.txt", "rank(close)\nrank(volume)\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert [s.expression for s in specs] == ["rank(close)", "rank(volume)"]

    def test_blank_lines_and_comments_skipped(self, tmp_path):
        path = write(tmp_path, "a.txt", "# comment\n\nrank(close)\n// also a comment\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert len(specs) == 1

    def test_inline_comments_are_not_stripped(self, tmp_path):
        # Guessing where a comment starts could silently corrupt an expression.
        path = write(tmp_path, "a.txt", "rank(close) # note\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert specs[0].expression == "rank(close) # note"

    def test_whitespace_variants_collapse_to_one_alpha(self, tmp_path):
        path = write(tmp_path, "a.txt", "rank(ts_delta(close, 5))\nrank( ts_delta(close,5) )\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert len(specs) == 1

    def test_names_are_generated_when_absent(self, tmp_path):
        path = write(tmp_path, "a.txt", "rank(close)\n")
        spec = load_alphas(path, default_settings=DEFAULTS)[0]
        assert spec.name and spec.name.startswith("alpha_")

    def test_name_is_stable_across_loads(self, tmp_path):
        path = write(tmp_path, "a.txt", "rank(close)\n")
        first = load_alphas(path, default_settings=DEFAULTS)[0].name
        second = load_alphas(path, default_settings=DEFAULTS)[0].name
        assert first == second

    def test_empty_file_raises(self, tmp_path):
        path = write(tmp_path, "a.txt", "# nothing but comments\n")
        with pytest.raises(ConfigError):
            load_alphas(path, default_settings=DEFAULTS)

    def test_utf8_bom_is_tolerated(self, tmp_path):
        path = tmp_path / "bom.txt"
        path.write_bytes("\ufeffrank(close)\n".encode("utf-8"))
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert specs[0].expression == "rank(close)"


class TestCsvLoading:
    def test_name_and_expression_columns(self, tmp_path):
        path = write(
            tmp_path, "a.csv",
            'name,expression\nalpha_001,"rank(ts_delta(close, 5))"\nalpha_002,"rank(volume)"\n',
        )
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert [s.name for s in specs] == ["alpha_001", "alpha_002"]
        assert specs[0].expression == "rank(ts_delta(close, 5))"

    def test_expression_only_column(self, tmp_path):
        path = write(tmp_path, "a.csv", "expression\nrank(close)\nrank(volume)\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert len(specs) == 2
        assert all(s.name for s in specs)

    def test_alternative_expression_column_names(self, tmp_path):
        for column in ("code", "formula", "regular"):
            path = write(tmp_path, f"{column}.csv", f"{column}\nrank(close)\n")
            assert len(load_alphas(path, default_settings=DEFAULTS)) == 1

    def test_single_unnamed_column_is_treated_as_expression(self, tmp_path):
        path = write(tmp_path, "a.csv", "whatever\nrank(close)\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert specs[0].expression == "rank(close)"

    def test_missing_expression_column_raises_with_the_header(self, tmp_path):
        path = write(tmp_path, "a.csv", "name,formula_text\nx,rank(close)\n")
        with pytest.raises(ConfigError) as excinfo:
            load_alphas(path, default_settings=DEFAULTS)
        assert "expression column" in str(excinfo.value)

    def test_per_row_settings_override_the_defaults(self, tmp_path):
        path = write(
            tmp_path, "a.csv",
            "name,expression,region,delay\na1,rank(close),CHN,0\na2,rank(volume),USA,1\n",
        )
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert specs[0].settings["region"] == "CHN"
        assert specs[0].settings["delay"] == 0
        assert specs[1].settings["region"] == "USA"

    def test_numeric_settings_columns_are_coerced(self, tmp_path):
        path = write(tmp_path, "a.csv", "expression,delay,truncation,visualization\nrank(close),1,0.08,true\n")
        settings = load_alphas(path, default_settings=DEFAULTS)[0].settings
        assert settings["delay"] == 1
        assert settings["truncation"] == pytest.approx(0.08)
        assert settings["visualization"] is True

    def test_same_expression_with_different_row_settings_keeps_both(self, tmp_path):
        path = write(
            tmp_path, "a.csv",
            "expression,region\nrank(close),USA\nrank(close),CHN\n",
        )
        assert len(load_alphas(path, default_settings=DEFAULTS)) == 2

    def test_blank_rows_are_skipped(self, tmp_path):
        path = write(tmp_path, "a.csv", "name,expression\na1,rank(close)\na2,\n,rank(volume)\n")
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert len(specs) == 2

    def test_header_only_csv_raises(self, tmp_path):
        path = write(tmp_path, "a.csv", "name,expression\n")
        with pytest.raises(ConfigError):
            load_alphas(path, default_settings=DEFAULTS)


class TestJsonLoading:
    def test_list_of_strings(self, tmp_path):
        path = write(tmp_path, "a.json", json.dumps(["rank(close)", "rank(volume)"]))
        specs = load_alphas(path, default_settings=DEFAULTS)
        assert [s.expression for s in specs] == ["rank(close)", "rank(volume)"]

    def test_list_of_objects(self, tmp_path):
        payload = [{"name": "x", "expression": "rank(close)", "settings": {"region": "CHN"}}]
        path = write(tmp_path, "a.json", json.dumps(payload))
        spec = load_alphas(path, default_settings=DEFAULTS)[0]
        assert spec.name == "x"
        assert spec.settings["region"] == "CHN"

    def test_wrapped_in_an_alphas_key(self, tmp_path):
        path = write(tmp_path, "a.json", json.dumps({"alphas": ["rank(close)"]}))
        assert len(load_alphas(path, default_settings=DEFAULTS)) == 1

    def test_invalid_json_raises(self, tmp_path):
        path = write(tmp_path, "a.json", "{not json")
        with pytest.raises(ConfigError):
            load_alphas(path, default_settings=DEFAULTS)

    def test_unsupported_entry_type_raises(self, tmp_path):
        path = write(tmp_path, "a.json", json.dumps([1, 2]))
        with pytest.raises(ConfigError):
            load_alphas(path, default_settings=DEFAULTS)


class TestFileHandling:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            load_alphas(tmp_path / "nope.csv", default_settings=DEFAULTS)
        assert "not found" in str(excinfo.value)

    def test_unsupported_extension_raises(self, tmp_path):
        path = write(tmp_path, "a.xlsx", "rank(close)")
        with pytest.raises(ConfigError) as excinfo:
            load_alphas(path, default_settings=DEFAULTS)
        assert "unsupported" in str(excinfo.value)

    def test_extensionless_file_is_read_as_text(self, tmp_path):
        path = write(tmp_path, "alphas", "rank(close)\n")
        assert len(load_alphas(path, default_settings=DEFAULTS)) == 1

    def test_settings_are_merged_over_the_defaults(self, tmp_path):
        path = write(tmp_path, "a.txt", "rank(close)\n")
        spec = load_alphas(path, default_settings={"region": "USA", "delay": 1})[0]
        assert spec.settings["region"] == "USA"
        # normalize_settings fills in everything BRAIN requires.
        assert spec.settings["language"] == "FASTEXPR"
        assert spec.settings["instrumentType"] == "EQUITY"


class TestSpecsFromExpressions:
    def test_builds_specs_with_generated_names(self):
        specs = specs_from_expressions(["rank(close)"], settings=DEFAULTS)
        assert len(specs) == 1
        assert specs[0].name.startswith("alpha_")
        assert specs[0].settings["region"] == "USA"

    def test_skips_blank_entries(self):
        specs = specs_from_expressions(["rank(close)", "  ", ""], settings=DEFAULTS)
        assert len(specs) == 1

    def test_strips_surrounding_whitespace(self):
        specs = specs_from_expressions(["  rank(close)  "], settings=DEFAULTS)
        assert specs[0].expression == "rank(close)"

    def test_label_falls_back_to_the_expression(self):
        from worldquant.models import AlphaSpec

        assert AlphaSpec(expression="rank(close)").label == "rank(close)"
        assert AlphaSpec(expression="rank(close)", name="n").label == "n"
