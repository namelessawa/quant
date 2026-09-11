"""Simulation settings normalization and payload shape.

Includes a regression test for a bug found against the live BRAIN API: YAML 1.1
parses a bare ``ON``/``OFF`` as a boolean, so ``pasteurization: ON`` in
config.yaml was sent as JSON ``true`` and rejected with
``400 {'settings': {'pasteurization': ['"True" is not a valid choice.']}}``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from conftest import YEARLY_RECORDSET
from worldquant.api import (
    ALPHA_CHECK_SUFFIX,
    DEFAULT_SETTINGS,
    SUBMISSION_CHECKS,
    AlphaGrade,
    all_checks_passed,
    alpha_check_url,
    build_simulation_payload,
    canonical_settings_json,
    failed_check_reasons,
    normalize_settings,
    parse_alpha_payload,
    parse_check_payload,
    parse_simulation_progress,
    parse_stage_block,
    parse_yearly_stats,
    passed_check_count,
    simulation_id_from_location,
)
from worldquant.config import PROJECT_ROOT
from worldquant.exceptions import ConfigError


class TestOnOffCoercion:
    """BRAIN wants the literal strings "ON"/"OFF", never JSON booleans."""

    @pytest.mark.parametrize("value,expected", [(True, "ON"), (False, "OFF")])
    def test_booleans_become_on_off_strings(self, value, expected):
        assert normalize_settings({"pasteurization": value})["pasteurization"] == expected
        assert normalize_settings({"nanHandling": value})["nanHandling"] == expected

    def test_yaml_bare_on_off_is_rescued(self):
        # This is exactly what config.yaml produced before the fix.
        loaded = yaml.safe_load("pasteurization: ON\nnanHandling: OFF\n")
        assert loaded == {"pasteurization": True, "nanHandling": False}, (
            "PyYAML still parses bare ON/OFF as booleans; the normalizer must cope"
        )
        normalized = normalize_settings(loaded)
        assert normalized["pasteurization"] == "ON"
        assert normalized["nanHandling"] == "OFF"

    def test_no_boolean_reaches_the_wire_payload(self):
        loaded = yaml.safe_load("pasteurization: ON\nnanHandling: OFF\n")
        payload = build_simulation_payload("rank(close)", loaded)
        assert payload["settings"]["pasteurization"] == "ON"
        assert payload["settings"]["nanHandling"] == "OFF"
        # visualization is the one setting that genuinely is a boolean.
        assert payload["settings"]["visualization"] is False

    def test_strings_are_upper_cased_and_trimmed(self):
        normalized = normalize_settings({"pasteurization": " on ", "nanHandling": "off"})
        assert normalized["pasteurization"] == "ON"
        assert normalized["nanHandling"] == "OFF"

    def test_other_on_off_spellings_from_yaml(self):
        # YAML 1.1 also treats yes/no as booleans, so they need the same rescue.
        assert yaml.safe_load("pasteurization: yes\n") == {"pasteurization": True}
        assert normalize_settings({"pasteurization": "yes"})["pasteurization"] == "ON"
        assert normalize_settings(yaml.safe_load("pasteurization: no\n"))["pasteurization"] == "OFF"


class TestEnumSettings:
    def test_a_boolean_where_a_string_enum_is_expected_raises(self):
        with pytest.raises(ConfigError) as excinfo:
            normalize_settings({"unitHandling": True})
        assert "unitHandling" in str(excinfo.value)
        assert "quote" in str(excinfo.value)

    def test_strings_are_upper_cased(self):
        normalized = normalize_settings({"region": "usa", "language": "fastexpr"})
        assert normalized["region"] == "USA"
        assert normalized["language"] == "FASTEXPR"

    def test_snake_case_aliases_are_translated(self):
        normalized = normalize_settings({"unit_handling": "verify", "nan_handling": "on"})
        assert normalized["unitHandling"] == "VERIFY"
        assert normalized["nanHandling"] == "ON"
        assert "unit_handling" not in normalized
        assert "nan_handling" not in normalized


class TestNumericCoercion:
    def test_delay_string_becomes_int(self):
        assert normalize_settings({"delay": "1"})["delay"] == 1

    def test_truncation_string_becomes_float(self):
        assert normalize_settings({"truncation": "0.08"})["truncation"] == pytest.approx(0.08)

    def test_unparseable_truncation_is_left_alone(self):
        assert normalize_settings({"truncation": "wide"})["truncation"] == "wide"

    def test_visualization_strings_become_booleans(self):
        for text in ("true", "TRUE", "1", "yes", "on"):
            assert normalize_settings({"visualization": text})["visualization"] is True
        for text in ("false", "0", "no", "off"):
            assert normalize_settings({"visualization": text})["visualization"] is False


class TestDefaultsAndPassthrough:
    def test_defaults_are_complete_and_wire_ready(self):
        normalized = normalize_settings(None)
        assert normalized == DEFAULT_SETTINGS
        assert normalized["instrumentType"] == "EQUITY"
        assert normalized["language"] == "FASTEXPR"
        assert isinstance(normalized["pasteurization"], str)
        assert isinstance(normalized["visualization"], bool)

    def test_none_values_do_not_erase_defaults(self):
        assert normalize_settings({"region": None})["region"] == "USA"

    def test_unknown_keys_pass_through(self):
        assert normalize_settings({"someFutureFlag": 7})["someFutureFlag"] == 7

    def test_canonical_json_is_stable_under_key_order(self):
        assert canonical_settings_json({"region": "USA", "delay": 1}) == canonical_settings_json(
            {"delay": 1, "region": "USA"}
        )


class TestPayloadShape:
    def test_matches_the_verified_contract(self):
        payload = build_simulation_payload("rank(ts_delta(close, 5))", DEFAULT_SETTINGS)
        assert set(payload) == {"type", "regular", "settings"}
        assert payload["type"] == "REGULAR"
        assert payload["regular"] == "rank(ts_delta(close, 5))"
        assert payload["settings"]["region"] == "USA"

    def test_expression_is_sent_verbatim_not_normalized(self):
        # Whitespace collapsing is for hashing only; the wire payload must carry
        # exactly what the user wrote.
        payload = build_simulation_payload("rank( close ,  5 )", DEFAULT_SETTINGS)
        assert payload["regular"] == "rank( close ,  5 )"


class TestLocationParsing:
    @pytest.mark.parametrize(
        "location,expected",
        [
            ("https://api.worldquantbrain.com/simulations/abc123", "abc123"),
            ("https://api.worldquantbrain.com/simulations/abc123/", "abc123"),
            ("  https://api.worldquantbrain.com/simulations/abc123  ", "abc123"),
            ("/simulations/abc123", "abc123"),
        ],
    )
    def test_extracts_the_trailing_id(self, location, expected):
        assert simulation_id_from_location(location) == expected

    def test_empty_location(self):
        assert simulation_id_from_location("") == ""
        assert simulation_id_from_location("   ") == ""


class TestShippedConfigFile:
    def test_config_yaml_yields_wire_ready_settings(self):
        """The shipped config must produce a payload BRAIN accepts.

        This is the guard that would have caught the 400 before it reached the
        live API.
        """
        raw = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
        normalized = normalize_settings(raw["settings"])

        assert normalized["pasteurization"] == "ON"
        assert normalized["nanHandling"] == "OFF"
        assert normalized["unitHandling"] == "VERIFY"
        assert normalized["language"] == "FASTEXPR"
        assert normalized["visualization"] is False
        assert normalized["delay"] == 1
        assert normalized["truncation"] == pytest.approx(0.08)

        # No ON/OFF-style setting may ever be serialized as a JSON boolean.
        for key in ("pasteurization", "nanHandling", "unitHandling", "region",
                    "universe", "neutralization", "instrumentType", "language"):
            assert not isinstance(normalized[key], bool), f"{key} must not be a boolean"
            assert isinstance(normalized[key], str), f"{key} must be a string"


class TestProgressParsing:
    def test_running(self):
        parsed = parse_simulation_progress({"progress": 0.5})
        assert parsed["status"] == "RUNNING"
        assert parsed["progress"] == 0.5

    def test_completed(self):
        parsed = parse_simulation_progress({"progress": 1.0, "alpha": "Xp2Kd"})
        assert parsed["status"] == "COMPLETED"
        assert parsed["alpha_id"] == "Xp2Kd"

    def test_failed(self):
        parsed = parse_simulation_progress({"status": "FAIL", "message": "bad expr"})
        assert parsed["status"] == "FAILED"
        assert parsed["message"] == "bad expr"

    def test_non_dict_payload(self):
        assert parse_simulation_progress("nope")["status"] == "REQUEST_ERROR"


class TestAlphaParsing:
    def test_full_is_block(self):
        parsed = parse_alpha_payload(
            {"id": "X9", "is": {"sharpe": 1.41, "turnover": 0.423, "longCount": 1520}}
        )
        assert parsed["sharpe"] == pytest.approx(1.41)
        assert parsed["turnover"] == pytest.approx(0.423)
        assert parsed["long_count"] == 1520
        assert parsed["fitness"] is None

    def test_missing_is_block(self):
        parsed = parse_alpha_payload({"id": "X9"})
        assert parsed["alpha_id"] == "X9"
        assert all(parsed[key] is None for key in ("sharpe", "fitness", "turnover"))

    def test_grade_and_stage_are_extracted(self):
        parsed = parse_alpha_payload({"id": "X9", "grade": "INFERIOR", "stage": "IS"})
        assert parsed["grade"] == "INFERIOR"
        assert parsed["stage"] == "IS"

    def test_grade_is_normalized_to_upper_case(self):
        parsed = parse_alpha_payload({"id": "X9", "grade": " average ", "stage": "is"})
        assert parsed["grade"] == "AVERAGE"
        assert parsed["stage"] == "IS"

    def test_absent_grade_is_none(self):
        parsed = parse_alpha_payload({"id": "X9"})
        assert parsed["grade"] is None
        assert parsed["stage"] is None

    def test_non_string_grade_does_not_crash(self):
        parsed = parse_alpha_payload({"id": "X9", "grade": 3, "stage": ["IS"]})
        assert parsed["grade"] is None
        assert parsed["stage"] is None


class TestPeriodBlocks:
    """A testPeriod run splits IS into train/test; both must be captured."""

    TEST_BLOCK = {
        "sharpe": 0.42, "fitness": 0.13, "turnover": 0.0522, "returns": 0.0113,
        "drawdown": 0.083, "margin": -0.0004, "pnl": 1000.0, "bookSize": 20000000,
        "longCount": 1500, "shortCount": 1490, "startDate": "2022-12-31",
    }

    def test_parses_a_stage_block(self):
        parsed = parse_stage_block(self.TEST_BLOCK)
        assert parsed["sharpe"] == pytest.approx(0.42)
        assert parsed["fitness"] == pytest.approx(0.13)
        assert parsed["turnover"] == pytest.approx(0.0522)
        assert parsed["long_count"] == 1500
        assert parsed["short_count"] == 1490
        assert parsed["start_date"] == "2022-12-31"

    def test_absent_block_is_none(self):
        assert parse_stage_block(None) is None

    def test_non_dict_block_is_none(self):
        assert parse_stage_block("nope") is None
        assert parse_stage_block([1, 2]) is None

    def test_missing_metrics_are_none(self):
        parsed = parse_stage_block({"sharpe": 1.0})
        assert parsed["sharpe"] == pytest.approx(1.0)
        assert parsed["fitness"] is None
        assert parsed["start_date"] is None

    def test_numeric_strings_are_coerced(self):
        parsed = parse_stage_block({"sharpe": "1.25", "longCount": "1500"})
        assert parsed["sharpe"] == pytest.approx(1.25)
        assert parsed["long_count"] == 1500

    def test_alpha_payload_surfaces_train_and_test(self):
        parsed = parse_alpha_payload({
            "id": "E5vraNQ1",
            "grade": "INFERIOR",
            "is": {"sharpe": -0.34, "fitness": -0.09},
            "train": {"sharpe": -0.53, "fitness": -0.18, "startDate": "2019-01-01"},
            "test": self.TEST_BLOCK,
        })
        assert parsed["train"]["sharpe"] == pytest.approx(-0.53)
        assert parsed["test"]["sharpe"] == pytest.approx(0.42)
        assert parsed["test"]["start_date"] == "2022-12-31"
        # The IS block still drives the grade and stays separate.
        assert parsed["sharpe"] == pytest.approx(-0.34)

    def test_no_test_period_leaves_blocks_none(self):
        parsed = parse_alpha_payload({"id": "X9", "is": {"sharpe": 1.4}})
        assert parsed["train"] is None
        assert parsed["test"] is None

    def test_test_block_diverging_from_is_is_preserved(self):
        # A held-out year that contradicts the in-sample result is exactly the
        # signal worth keeping, so it must not be smoothed or reconciled away.
        parsed = parse_alpha_payload({
            "id": "X9",
            "is": {"sharpe": 2.20, "fitness": 2.07},
            "test": {"sharpe": -1.50, "fitness": -0.80},
        })
        assert parsed["sharpe"] == pytest.approx(2.20)
        assert parsed["test"]["sharpe"] == pytest.approx(-1.50)


class TestAlphaGrade:
    def test_observed_vocabulary(self):
        assert AlphaGrade.OBSERVED_ORDER == (
            "INFERIOR", "AVERAGE", "GOOD", "EXCELLENT", "SPECTACULAR",
        )

    def test_rank_orders_worst_to_best(self):
        ranks = [AlphaGrade.rank(g) for g in AlphaGrade.OBSERVED_ORDER]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks), "each tier must rank distinctly"
        assert AlphaGrade.rank("INFERIOR") < AlphaGrade.rank("AVERAGE")
        assert AlphaGrade.rank("AVERAGE") < AlphaGrade.rank("GOOD")
        assert AlphaGrade.rank("GOOD") < AlphaGrade.rank("EXCELLENT")
        assert AlphaGrade.rank("EXCELLENT") < AlphaGrade.rank("SPECTACULAR")

    def test_spectacular_is_the_best_known_grade(self):
        # It was returned by a live run before the code knew the tier existed,
        # and rank() scored it -1 — worse than INFERIOR.
        assert AlphaGrade.rank("SPECTACULAR") == max(
            AlphaGrade.rank(g) for g in AlphaGrade.OBSERVED_ORDER
        )
        assert AlphaGrade.rank("SPECTACULAR") > 0

    def test_rank_is_case_and_space_insensitive(self):
        assert AlphaGrade.rank(" average ") == AlphaGrade.rank("AVERAGE")
        assert AlphaGrade.rank("excellent") == AlphaGrade.rank("EXCELLENT")
        assert AlphaGrade.rank("spectacular") == AlphaGrade.rank("SPECTACULAR")

    def test_unknown_and_missing_grades_rank_lowest(self):
        assert AlphaGrade.rank(None) == -1
        assert AlphaGrade.rank("") == -1
        assert AlphaGrade.rank("SOMETHING_NEW") == -1
        assert AlphaGrade.rank("SOMETHING_NEW") < AlphaGrade.rank("INFERIOR")


class TestYearlyParsing:
    """The live endpoint returns a JSON recordset, not CSV."""

    def test_parses_the_verified_shape(self):
        stats = parse_yearly_stats(YEARLY_RECORDSET)
        assert set(stats) == {"2019", "2020", "2021", "2022", "2023"}
        assert stats["2019"]["sharpe"] == pytest.approx(1.10)
        assert stats["2020"]["sharpe"] == pytest.approx(-0.40)
        assert stats["2019"]["turnover"] == pytest.approx(0.40)
        assert stats["2019"]["fitness"] == pytest.approx(0.90)

    def test_percent_columns_stay_decimal_fractions(self):
        stats = parse_yearly_stats(YEARLY_RECORDSET)
        # 0.585 means 58.5%; the aggregate `is` block uses the same convention.
        assert stats["2020"]["turnover"] == pytest.approx(0.50)
        assert stats["2020"]["returns"] == pytest.approx(-0.0337)

    def test_camel_case_columns_become_snake_case(self):
        stats = parse_yearly_stats(YEARLY_RECORDSET)
        assert stats["2019"]["book_size"] == pytest.approx(20000000)
        assert stats["2019"]["long_count"] == 1551
        assert stats["2019"]["short_count"] == 1556
        assert "bookSize" not in stats["2019"]

    def test_columns_are_mapped_by_schema_name_not_position(self):
        # A reordered schema must not shift values into the wrong metrics.
        reordered = {
            "schema": {
                "properties": [
                    {"name": "sharpe", "type": "decimal"},
                    {"name": "year", "type": "year"},
                    {"name": "turnover", "type": "percent"},
                ]
            },
            "records": [[2.5, "2021", 0.31]],
        }
        stats = parse_yearly_stats(reordered)
        assert stats["2021"]["sharpe"] == pytest.approx(2.5)
        assert stats["2021"]["turnover"] == pytest.approx(0.31)

    def test_out_of_sample_rows_are_dropped(self):
        payload = {
            "schema": {
                "properties": [
                    {"name": "year", "type": "year"},
                    {"name": "sharpe", "type": "decimal"},
                    {"name": "stage", "type": "string"},
                ]
            },
            "records": [["2019", 1.1, "IS"], ["2024", 3.0, "OS"]],
        }
        stats = parse_yearly_stats(payload)
        assert set(stats) == {"2019"}, "only in-sample rows match the aggregate IS metrics"

    def test_train_rows_are_in_sample_when_a_test_period_was_used(self):
        # Live regression: with testPeriod=P1Y BRAIN labels the in-sample years
        # TRAIN and the held-out year TEST. Filtering on "IS" alone dropped every
        # row, so two whole rounds lost their yearly stats while the endpoint
        # kept returning a valid, fully populated recordset.
        payload = {
            "schema": {
                "properties": [
                    {"name": "year", "type": "year"},
                    {"name": "sharpe", "type": "decimal"},
                    {"name": "stage", "type": "string"},
                ]
            },
            "records": [
                ["2019", 3.06, "TRAIN"], ["2020", 1.44, "TRAIN"],
                ["2021", 3.85, "TRAIN"], ["2022", 2.96, "TRAIN"],
                ["2023", 0.71, "TEST"],
            ],
        }
        stats = parse_yearly_stats(payload)
        assert set(stats) == {"2019", "2020", "2021", "2022"}
        assert stats["2019"]["sharpe"] == pytest.approx(3.06)

    def test_stage_label_matching_is_case_insensitive(self):
        payload = {
            "schema": {"properties": [{"name": "year"}, {"name": "sharpe"}, {"name": "stage"}]},
            "records": [["2019", 1.1, " train "], ["2023", 0.4, " test "]],
        }
        assert set(parse_yearly_stats(payload)) == {"2019"}

    def test_missing_stage_column_keeps_every_row(self):
        payload = {
            "schema": {"properties": [{"name": "year"}, {"name": "sharpe"}]},
            "records": [["2019", 1.1], ["2020", -0.4]],
        }
        assert set(parse_yearly_stats(payload)) == {"2019", "2020"}

    def test_full_dates_reduce_to_the_year(self):
        payload = {
            "schema": {"properties": [{"name": "year"}, {"name": "sharpe"}]},
            "records": [["2019-01-01", 1.1]],
        }
        assert "2019" in parse_yearly_stats(payload)

    def test_short_rows_are_skipped_not_crashing(self):
        payload = {
            "schema": {"properties": [{"name": "year"}, {"name": "sharpe"}]},
            "records": [["2019"], "not-a-row", None, ["2020", 0.5]],
        }
        stats = parse_yearly_stats(payload)
        assert stats["2020"]["sharpe"] == pytest.approx(0.5)
        assert stats["2019"]["sharpe"] is None

    def test_numeric_strings_are_coerced(self):
        payload = {
            "schema": {"properties": [{"name": "year"}, {"name": "sharpe"}]},
            "records": [["2019", "1.25"]],
        }
        assert parse_yearly_stats(payload)["2019"]["sharpe"] == pytest.approx(1.25)

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            None,
            "<html>nope</html>",
            [],
            {},
            {"schema": {}, "records": []},
            {"records": [["2019", 1.1]]},
            {"schema": {"properties": [{"name": "sharpe"}]}, "records": [[1.1]]},
            {"schema": {"properties": "not-a-list"}, "records": []},
            {"schema": {"properties": [{"name": "year"}]}, "records": "not-a-list"},
        ],
    )
    def test_unparseable_payload_is_empty(self, payload):
        assert parse_yearly_stats(payload) == {}

    def test_empty_records_is_empty(self):
        payload = {"schema": {"properties": [{"name": "year"}]}, "records": []}
        assert parse_yearly_stats(payload) == {}


#: Shaped like the live ``GET /alphas/{id}/check`` body observed for the
#: SPECTACULAR alpha ``2rOn70lb``: everything nested under ``is``, eight checks,
#: and a ``selfCorrelated`` recordset naming the alphas it correlates with.
CHECK_PAYLOAD: dict[str, Any] = {
    "is": {
        "checks": [
            {"name": "LOW_SHARPE", "value": 2.28, "result": "PASS", "limit": 1.25},
            {"name": "LOW_FITNESS", "value": 2.69, "result": "PASS", "limit": 1.0},
            {"name": "LOW_TURNOVER", "value": 0.0478, "result": "PASS", "limit": 0.01},
            {"name": "HIGH_TURNOVER", "value": 0.0478, "result": "PASS", "limit": 0.7},
            {"name": "CONCENTRATED_WEIGHT", "value": 0.0079, "result": "PASS", "limit": None},
            {"name": "LOW_SUB_UNIVERSE_SHARPE", "value": 0.98, "result": "PASS", "limit": 0.98},
            {"name": "SELF_CORRELATION", "value": 0.6996, "result": "PASS", "limit": 0.7},
            {
                "name": "MATCHES_COMPETITION", "value": None, "result": "PASS",
                "competitions": [],
            },
        ],
        "selfCorrelated": {
            "max": 0.6996,
            "schema": {"properties": [{"name": "alpha_id"}, {"name": "correlation"}]},
            "records": [["2rOn70lb", 0.6996]],
        },
    },
}


def _copy(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


def _with_results(**overrides: str) -> dict[str, Any]:
    """CHECK_PAYLOAD with selected check verdicts replaced."""
    payload = _copy(CHECK_PAYLOAD)
    for check in payload["is"]["checks"]:
        if check["name"] in overrides:
            check["result"] = overrides[check["name"]]
    return payload


class TestParseCheckPayload:
    def test_all_eight_checks_are_parsed(self):
        parsed = parse_check_payload(CHECK_PAYLOAD)
        assert parsed["total"] == 8
        assert parsed["passed_count"] == 8
        assert parsed["all_passed"] is True
        assert parsed["failures"] == []
        assert set(parsed["checks"]) == set(SUBMISSION_CHECKS)

    def test_a_check_keeps_its_value_and_limit(self):
        checks = parse_check_payload(CHECK_PAYLOAD)["checks"]
        assert checks["SELF_CORRELATION"] == {
            "result": "PASS", "limit": 0.7, "value": 0.6996, "competitions": None,
        }

    def test_self_correlation_is_read_from_the_recordset(self):
        parsed = parse_check_payload(CHECK_PAYLOAD)
        assert parsed["self_correlation"] == pytest.approx(0.6996)
        assert parsed["self_correlated_with"] == [
            {"alpha_id": "2rOn70lb", "correlation": 0.6996}
        ]

    def test_recordset_supplies_the_correlation_when_max_is_absent(self):
        payload = _copy(CHECK_PAYLOAD)
        del payload["is"]["selfCorrelated"]["max"]
        assert parse_check_payload(payload)["self_correlation"] == pytest.approx(0.6996)

    def test_the_worst_correlation_wins(self):
        payload = _copy(CHECK_PAYLOAD)
        payload["is"]["selfCorrelated"]["records"] = [["A", 0.31], ["B", 0.68]]
        payload["is"]["selfCorrelated"]["max"] = 0.31
        assert parse_check_payload(payload)["self_correlation"] == pytest.approx(0.68)

    def test_missing_schema_falls_back_to_the_observed_column_order(self):
        payload = _copy(CHECK_PAYLOAD)
        correlated = payload["is"]["selfCorrelated"]
        del correlated["schema"]
        correlated["records"] = [[
            "2rOn70lb", "my alpha", "EQUITY", "USA", "TOP3000",
            0.6996, 2.28, 0.0478, 0.0725, 2.69, 0.0004,
        ]]
        parsed = parse_check_payload(payload)
        assert parsed["self_correlation"] == pytest.approx(0.6996)
        assert parsed["self_correlated_with"][0]["alpha_id"] == "2rOn70lb"

    def test_checks_at_the_top_level_are_tolerated(self):
        parsed = parse_check_payload(CHECK_PAYLOAD["is"])
        assert parsed["total"] == 8
        assert parsed["all_passed"] is True

    def test_a_failing_check_is_reported_with_its_limit(self):
        parsed = parse_check_payload(_with_results(SELF_CORRELATION="FAIL"))
        assert parsed["all_passed"] is False
        assert parsed["passed_count"] == 7
        assert parsed["failures"] == ["SELF_CORRELATION=0.6996 (limit 0.7): FAIL"]

    def test_pending_is_not_a_pass(self):
        # The reason the check endpoint has to be called at all: the plain alpha
        # payload reports SELF_CORRELATION as PENDING indefinitely.
        parsed = parse_check_payload(_with_results(SELF_CORRELATION="PENDING"))
        assert parsed["all_passed"] is False
        assert parsed["failures"] == ["SELF_CORRELATION=0.6996 (limit 0.7): PENDING"]

    def test_a_check_without_a_result_is_not_a_pass(self):
        assert parse_check_payload(_with_results(LOW_FITNESS=""))["all_passed"] is False

    def test_checks_without_a_name_are_skipped(self):
        payload = _copy(CHECK_PAYLOAD)
        payload["is"]["checks"].append({"result": "PASS"})
        payload["is"]["checks"].append("not-a-dict")
        assert parse_check_payload(payload)["total"] == 8

    def test_a_non_list_checks_block_yields_no_checks(self):
        payload = {"is": {"checks": {"LOW_SHARPE": "PASS"}}}
        assert parse_check_payload(payload)["total"] == 0

    def test_a_recordset_without_records_leaves_correlation_unset(self):
        payload = _copy(CHECK_PAYLOAD)
        payload["is"]["selfCorrelated"] = {"records": []}
        parsed = parse_check_payload(payload)
        assert parsed["self_correlation"] is None
        assert parsed["self_correlated_with"] == []

    def test_a_short_record_pads_missing_columns_with_none(self):
        payload = _copy(CHECK_PAYLOAD)
        payload["is"]["selfCorrelated"]["records"] = [["A"]]
        parsed = parse_check_payload(payload)
        assert parsed["self_correlated_with"] == [{"alpha_id": "A", "correlation": None}]
        assert parsed["self_correlation"] == pytest.approx(0.6996)

    @pytest.mark.parametrize("payload", [None, "", [], "nope", 42, {"is": "nope"}])
    def test_unparseable_payload_is_the_empty_shape(self, payload):
        assert parse_check_payload(payload) == {
            "checks": {}, "self_correlation": None, "self_correlated_with": [],
            "passed_count": 0, "total": 0, "all_passed": False, "failures": [],
        }


class TestAllChecksPassed:
    def test_eight_passes_is_a_pass(self):
        checks = parse_check_payload(CHECK_PAYLOAD)["checks"]
        assert all_checks_passed(checks) is True

    def test_seven_passes_and_one_missing_is_not(self):
        # A partial response must never be read as acceptance.
        checks = parse_check_payload(CHECK_PAYLOAD)["checks"]
        checks.pop("SELF_CORRELATION")
        assert all_checks_passed(checks) is False

    def test_one_failure_is_not_a_pass(self):
        checks = parse_check_payload(_with_results(HIGH_TURNOVER="FAIL"))["checks"]
        assert all_checks_passed(checks) is False

    def test_an_extra_check_does_not_rescue_a_partial_response(self):
        checks = parse_check_payload(CHECK_PAYLOAD)["checks"]
        checks.pop("MATCHES_COMPETITION")
        checks["SOMETHING_NEW"] = {"result": "PASS"}
        assert len(checks) == len(SUBMISSION_CHECKS)
        assert all_checks_passed(checks) is False

    @pytest.mark.parametrize("checks", [None, {}, [], "nope"])
    def test_nothing_resolved_is_not_a_pass(self, checks):
        assert all_checks_passed(checks) is False

    def test_expected_can_be_narrowed_to_a_subset(self):
        checks = {"LOW_SHARPE": {"result": "PASS"}}
        assert all_checks_passed(checks, expected=("LOW_SHARPE",)) is True
        assert all_checks_passed(checks) is False

    def test_a_required_check_that_is_not_a_dict_vetoes_the_verdict(self):
        # It cannot be verified, and an unverifiable required check is not a pass.
        checks = {name: {"result": "PASS"} for name in SUBMISSION_CHECKS}
        checks["LOW_SHARPE"] = "PASS"
        assert all_checks_passed(checks) is False

    def test_an_unexpected_check_must_also_pass(self):
        checks = {name: {"result": "PASS"} for name in SUBMISSION_CHECKS}
        checks["SOMETHING_NEW"] = {"result": "FAIL"}
        assert all_checks_passed(checks) is False

    def test_verdict_case_is_normalized(self):
        checks = {name: {"result": "pass"} for name in SUBMISSION_CHECKS}
        assert all_checks_passed(checks) is True


class TestFailedCheckReasons:
    def test_only_failures_are_listed(self):
        checks = parse_check_payload(_with_results(LOW_FITNESS="FAIL"))["checks"]
        assert failed_check_reasons(checks) == ["LOW_FITNESS=2.69 (limit 1.0): FAIL"]

    def test_value_without_a_limit(self):
        assert failed_check_reasons({"X": {"result": "FAIL", "value": 0.4}}) == ["X=0.4: FAIL"]

    def test_neither_value_nor_limit(self):
        assert failed_check_reasons({"X": {"result": "FAIL"}}) == ["X: FAIL"]

    def test_a_missing_result_says_so(self):
        assert failed_check_reasons({"X": {"value": 1}}) == ["X=1: NO RESULT"]

    def test_a_pass_is_not_a_reason(self):
        assert failed_check_reasons({"X": {"result": "pass", "value": 1}}) == []

    @pytest.mark.parametrize("checks", [None, {}])
    def test_nothing_checked_has_no_reasons(self, checks):
        assert failed_check_reasons(checks) == []

    def test_non_dict_entries_are_skipped(self):
        reasons = failed_check_reasons({"X": "FAIL", "Y": {"result": "FAIL"}})
        assert reasons == ["Y: FAIL"]


class TestPassedCheckCount:
    def test_counts_only_passes(self):
        checks = parse_check_payload(
            _with_results(LOW_FITNESS="FAIL", SELF_CORRELATION="PENDING")
        )["checks"]
        assert passed_check_count(checks) == 6

    @pytest.mark.parametrize("checks", [None, {}])
    def test_nothing_resolved_counts_zero(self, checks):
        assert passed_check_count(checks) == 0

    def test_an_unexpected_check_counts_too(self):
        assert passed_check_count({"X": {"result": "PASS"}}) == 1


class TestCheckUrl:
    def test_appends_the_check_suffix_to_the_alpha_url(self):
        url = alpha_check_url("https://api.worldquantbrain.com", "2rOn70lb")
        assert url == "https://api.worldquantbrain.com/alphas/2rOn70lb/check"

    def test_the_suffix_matches_the_live_endpoint(self):
        assert ALPHA_CHECK_SUFFIX == "/check"


class TestSubmissionCheckNames:
    def test_the_eight_expected_names(self):
        assert SUBMISSION_CHECKS == (
            "LOW_SHARPE", "LOW_FITNESS", "LOW_TURNOVER", "HIGH_TURNOVER",
            "CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "SELF_CORRELATION",
            "MATCHES_COMPETITION",
        )

    def test_the_count_is_the_one_the_gate_quotes(self):
        assert len(SUBMISSION_CHECKS) == 8
