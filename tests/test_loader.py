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


class TestAblationMetadata:
    """Optional ablation provenance columns/keys (spec: ablation full chain)."""

    def test_plain_rows_have_no_ablation_metadata(self, tmp_path):
        path = write(tmp_path, "a.csv", "name,expression\nx,rank(close)\n")
        spec = load_alphas(path, default_settings=DEFAULTS)[0]
        assert spec.source is None
        assert spec.ablation_group_id is None
        assert spec.changed_parameters is None
        assert spec.parent_experiment_id is None
        assert spec.is_ablation is False

    def test_csv_reads_full_ablation_metadata_json_changes(self, tmp_path):
        path = write(
            tmp_path, "a.csv",
            "expression,source,ablation_group_id,changed_parameters,parent_experiment_id,decay\n"
            "rank(close),ablation,sweep-7,"
            '"{""decay"": 8, ""truncation"": 0.05}",42,8\n',
        )
        spec = load_alphas(path, default_settings=DEFAULTS)[0]
        assert spec.source == "ablation"
        assert spec.is_ablation is True
        assert spec.ablation_group_id == "sweep-7"
        assert spec.changed_parameters == {"decay": 8, "truncation": 0.05}
        assert spec.parent_experiment_id == 42
        assert spec.settings["decay"] == 8

    def test_csv_accepts_key_value_changed_parameters(self, tmp_path):
        path = write(
            tmp_path, "a.csv",
            "expression,source,changed_parameters\n"
            "rank(close),ablation,decay=8;neutralization=SUBINDUSTRY\n",
        )
        spec = load_alphas(path, default_settings=DEFAULTS)[0]
        assert spec.source == "ablation"
        assert spec.changed_parameters == {
            "decay": 8, "neutralization": "SUBINDUSTRY"
        }

    def test_json_reads_full_ablation_metadata(self, tmp_path):
        payload = [{
            "expression": "rank(close)",
            "source": "ablation",
            "ablation_group_id": "g1",
            "changed_parameters": {"decay": 12},
            "parent_experiment_id": 7,
        }]
        path = write(tmp_path, "a.json", json.dumps(payload))
        spec = load_alphas(path, default_settings=DEFAULTS)[0]
        assert spec.source == "ablation"
        assert spec.ablation_group_id == "g1"
        assert spec.changed_parameters == {"decay": 12}
        assert spec.parent_experiment_id == 7

    def test_bad_parent_experiment_id_raises(self, tmp_path):
        path = write(
            tmp_path, "a.csv",
            "expression,parent_experiment_id\nrank(close),not-an-int\n",
        )
        with pytest.raises(ConfigError):
            load_alphas(path, default_settings=DEFAULTS)

    def test_csv_ablation_specs_run_through_the_standard_gate(self, tmp_path):
        # Scenario: the standard AlphaSpec/CSV ablation path must survive
        # gate_specs end to end (same signal, different decay allowed; exact
        # experiment duplicates still rejected, --force overrides).
        from dataclasses import replace as dc_replace

        from worldquant.registry import FactorRegistry, RegistryConfig, gate_specs

        base = RegistryConfig()
        cfg = dc_replace(
            base,
            pre_simulation=dc_replace(base.pre_simulation, max_signal_experiments=1),
        )
        registry = FactorRegistry(tmp_path / "fr.db", cfg)
        try:
            baseline = write(
                tmp_path, "baseline.csv",
                'expression,decay\n"rank(ts_delta(close, 5))",4\n',
            )
            ablations = write(
                tmp_path, "ablations.csv",
                'expression,source,ablation_group_id,changed_parameters,decay\n'
                '"rank(ts_delta(close, 5))",ablation,s1,"{""decay"":8}",8\n'
                '"rank(ts_delta(close, 5))",ablation,s1,"{""decay"":16}",16\n',
            )
            base_specs = load_alphas(baseline, default_settings=DEFAULTS)
            sweep_specs = load_alphas(ablations, default_settings=DEFAULTS)
            assert gate_specs(registry, base_specs).blocked == 0
            result = gate_specs(registry, sweep_specs)
            assert len(result.kept) == 2
            assert result.blocked == 0

            # "Exact duplicate" means an experiment that was actually
            # researched: fold a completed result for the decay=8 variant
            # locally (no simulation), then replay it.
            from worldquant.api import SUBMISSION_CHECKS, SimulationStatus
            from worldquant.hashing import experiment_identity
            from worldquant.models import AlphaResult
            from worldquant.registry.adapter import record_completed
            import json

            decay8 = next(s for s in sweep_specs if s.settings["decay"] == 8)
            record_completed(
                registry,
                AlphaResult(
                    alpha_id="local-B8",
                    expression=decay8.expression,
                    dedup_key=experiment_identity(decay8.expression, decay8.settings),
                    settings_json=json.dumps(decay8.settings),
                    status=SimulationStatus.COMPLETED,
                    remote_alpha_id="BRA-B8",
                    grade="GOOD", passed=True,
                    sharpe=1.5, fitness=1.2,
                    submission_checks={
                        name: {"result": "PASS"} for name in SUBMISSION_CHECKS
                    },
                    self_correlation=0.2,
                ),
            )
            repeat = gate_specs(registry, [decay8])
            assert repeat.kept == []
            assert repeat.tally.get("REJECT_EXACT_DUPLICATE") == 1
            forced = gate_specs(registry, [decay8], force=True)
            assert len(forced.kept) == 1
        finally:
            registry.close()
