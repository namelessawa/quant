"""Yearly-stability statistics and the yearly filter rules."""

from __future__ import annotations

import pytest

from worldquant.api import SimulationStatus
from worldquant.config import FilterConfig
from worldquant.filters import YearlyStabilityFilter, evaluate_filters
from worldquant.models import AlphaResult, summarize_yearly

FIVE_YEARS = {
    "2019": {"sharpe": 1.1, "turnover": 0.4, "fitness": 0.9},
    "2020": {"sharpe": -0.4, "turnover": 0.5, "fitness": -0.2},
    "2021": {"sharpe": 0.9, "turnover": 0.4, "fitness": 0.7},
    "2022": {"sharpe": 1.3, "turnover": 0.3, "fitness": 1.1},
    "2023": {"sharpe": 0.7, "turnover": 0.6, "fitness": 0.5},
}


def make_result(**overrides) -> AlphaResult:
    base = dict(
        alpha_id="alpha_y",
        expression="rank(close)",
        dedup_key="k",
        status=SimulationStatus.COMPLETED,
        sharpe=1.5,
        fitness=1.2,
        turnover=0.40,
        yearly_stats=FIVE_YEARS,
    )
    base.update(overrides)
    return AlphaResult(**base)


class TestSummarizeYearly:
    def test_counts_and_ratio(self):
        summary = summarize_yearly(FIVE_YEARS)
        assert summary.total_years == 5
        assert summary.positive_years == 4
        assert summary.negative_years == 1
        assert summary.neutral_years == 0
        assert summary.positive_year_ratio == pytest.approx(0.8)

    def test_std_worst_and_best(self):
        summary = summarize_yearly(FIVE_YEARS)
        assert summary.yearly_sharpe_std == pytest.approx(0.6648300, abs=1e-6)
        assert summary.worst_year_sharpe == pytest.approx(-0.4)
        assert summary.best_year_sharpe == pytest.approx(1.3)

    def test_zero_sharpe_counts_as_neutral(self):
        summary = summarize_yearly({"2020": {"sharpe": 0.0}, "2021": {"sharpe": 1.0}})
        assert summary.positive_years == 1
        assert summary.negative_years == 0
        assert summary.neutral_years == 1
        assert summary.positive_year_ratio == pytest.approx(0.5)

    def test_single_year_has_zero_std(self):
        summary = summarize_yearly({"2021": {"sharpe": 1.0}})
        assert summary.total_years == 1
        assert summary.yearly_sharpe_std == 0.0
        assert summary.worst_year_sharpe == 1.0

    def test_years_without_sharpe_are_excluded_not_zero_filled(self):
        # Counting a missing year as 0.0 would silently drag the ratio down.
        partial = {
            "2019": {"sharpe": 1.0},
            "2020": {"turnover": 0.4},          # no sharpe at all
            "2021": {"sharpe": None},           # explicit null
            "2022": {"sharpe": "n/a"},          # non-numeric
            "2023": {"sharpe": 1.0},
        }
        summary = summarize_yearly(partial)
        assert summary.total_years == 2
        assert summary.positive_years == 2
        assert summary.positive_year_ratio == pytest.approx(1.0)

    def test_empty_inputs(self):
        assert summarize_yearly(None).is_empty is True
        assert summarize_yearly({}).is_empty is True
        assert summarize_yearly({"2020": {"turnover": 0.4}}).is_empty is True

    def test_malformed_rows_do_not_raise(self):
        summary = summarize_yearly({"2020": "not-a-dict", "2021": {"sharpe": 1.0}})  # type: ignore[dict-item]
        assert summary.total_years == 1

    def test_boolean_is_not_treated_as_a_number(self):
        summary = summarize_yearly({"2020": {"sharpe": True}})
        assert summary.is_empty is True

    def test_flat_dict_shape(self):
        flat = summarize_yearly(FIVE_YEARS).as_flat_dict()
        assert flat["positive_years"] == 4
        assert flat["worst_year_sharpe"] == pytest.approx(-0.4)
        assert set(flat) >= {
            "positive_years", "negative_years", "neutral_years", "total_years",
            "positive_year_ratio", "yearly_sharpe_std", "worst_year_sharpe",
            "best_year_sharpe",
        }


class TestYearlyStabilityFilter:
    def test_disabled_when_all_rules_are_none(self):
        config = FilterConfig(
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
            min_worst_year_sharpe=None, max_yearly_sharpe_std=None,
        )
        assert YearlyStabilityFilter(config).enabled is False
        assert YearlyStabilityFilter(config).evaluate(make_result()) == []

    def test_enabled_by_require_yearly_data_alone(self):
        config = FilterConfig(
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
            require_yearly_data=True,
        )
        assert YearlyStabilityFilter(config).enabled is True

    def test_ratio_violation_message(self):
        config = FilterConfig(min_positive_year_ratio=0.9, max_negative_sharpe_years=None)
        reasons = YearlyStabilityFilter(config).evaluate(make_result())
        assert reasons == ["Positive years 4/5 = 80.0% < 90.0%"]

    def test_ratio_at_the_limit_passes(self):
        config = FilterConfig(min_positive_year_ratio=0.8, max_negative_sharpe_years=None)
        assert YearlyStabilityFilter(config).evaluate(make_result()) == []

    def test_negative_year_count_violation(self):
        unstable = {
            "2019": {"sharpe": 2.0},
            "2020": {"sharpe": -0.5},
            "2021": {"sharpe": -0.8},
            "2022": {"sharpe": 1.0},
            "2023": {"sharpe": 0.9},
        }
        config = FilterConfig(min_positive_year_ratio=None, max_negative_sharpe_years=1)
        reasons = YearlyStabilityFilter(config).evaluate(make_result(yearly_stats=unstable))
        assert reasons == ["Negative-sharpe years 2 > 1"]

    def test_worst_year_sharpe_rule(self):
        config = FilterConfig(
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
            min_worst_year_sharpe=0.0,
        )
        reasons = YearlyStabilityFilter(config).evaluate(make_result())
        assert reasons == ["Worst year sharpe -0.40 < 0.00"]

    def test_yearly_std_rule(self):
        config = FilterConfig(
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
            max_yearly_sharpe_std=0.5,
        )
        reasons = YearlyStabilityFilter(config).evaluate(make_result())
        assert reasons == ["Yearly sharpe std 0.66 > 0.50"]

    def test_missing_yearly_data_is_skipped_by_default(self):
        config = FilterConfig(min_positive_year_ratio=0.9, max_negative_sharpe_years=0)
        assert YearlyStabilityFilter(config).evaluate(make_result(yearly_stats={})) == []

    def test_missing_yearly_data_fails_when_required(self):
        config = FilterConfig(
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
            require_yearly_data=True,
        )
        reasons = YearlyStabilityFilter(config).evaluate(make_result(yearly_stats={}))
        assert reasons == ["Yearly data missing but filters.require_yearly_data is enabled"]


class TestAggregateGoodButYearlyUnstable:
    """The case the yearly rules exist for: a decent headline Sharpe hiding a
    year-by-year record that is mostly negative."""

    def test_fails_on_yearly_rules_despite_good_aggregate(self):
        unstable = {
            "2019": {"sharpe": 4.0},
            "2020": {"sharpe": -1.0},
            "2021": {"sharpe": -0.8},
            "2022": {"sharpe": -0.3},
            "2023": {"sharpe": -0.5},
        }
        config = FilterConfig(min_positive_year_ratio=0.6, max_negative_sharpe_years=1)
        outcome = evaluate_filters(make_result(yearly_stats=unstable), config)
        assert outcome.passed is False
        assert any(reason.startswith("Positive years 1/5") for reason in outcome.reasons)
        assert "Negative-sharpe years 4 > 1" in outcome.reasons

    def test_passes_when_both_groups_are_satisfied(self):
        config = FilterConfig(min_positive_year_ratio=0.6, max_negative_sharpe_years=1)
        assert evaluate_filters(make_result(), config).passed is True
