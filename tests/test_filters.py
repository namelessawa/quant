"""Aggregate threshold filters and the overall filter verdict."""

from __future__ import annotations

from worldquant.api import SimulationStatus
from worldquant.config import FilterConfig
from worldquant.filters import ThresholdFilter, evaluate_filters
from worldquant.models import AlphaResult


def make_result(**overrides) -> AlphaResult:
    base = dict(
        alpha_id="alpha_x",
        expression="rank(close)",
        dedup_key="k",
        status=SimulationStatus.COMPLETED,
        sharpe=1.5,
        fitness=1.2,
        turnover=0.40,
        returns=0.12,
        drawdown=0.05,
        margin=0.0007,
    )
    base.update(overrides)
    return AlphaResult(**base)


DEFAULTS = FilterConfig(min_positive_year_ratio=None, max_negative_sharpe_years=None)


class TestThresholdFilter:
    def test_all_thresholds_met_passes(self):
        outcome = evaluate_filters(make_result(), DEFAULTS)
        assert outcome.passed is True
        assert outcome.reasons == []

    def test_reports_every_violation_not_just_the_first(self):
        result = make_result(sharpe=0.82, fitness=0.33, turnover=1.342)
        outcome = evaluate_filters(result, DEFAULTS)
        assert outcome.passed is False
        assert outcome.reasons == [
            "Sharpe 0.82 < 1.25",
            "Fitness 0.33 < 1.00",
            "Turnover 134.2% > 70.0%",
        ]

    def test_turnover_is_treated_as_a_decimal_fraction(self):
        # 0.70 from the API means 70% and must satisfy a 0.70 cap exactly.
        assert evaluate_filters(make_result(turnover=0.70), DEFAULTS).passed is True
        assert evaluate_filters(make_result(turnover=0.71), DEFAULTS).passed is False

    def test_float_noise_at_the_limit_does_not_fail(self):
        noisy = 0.4 + 0.1 + 0.1 + 0.1  # 0.7000000000000001
        outcome = evaluate_filters(make_result(turnover=noisy), DEFAULTS)
        assert outcome.passed is True, outcome.reasons

    def test_genuine_near_limit_violation_still_reports(self):
        outcome = evaluate_filters(make_result(turnover=0.7004), DEFAULTS)
        assert outcome.passed is False
        assert len(outcome.reasons) == 1
        # The rendered value and limit must not be identical, or the message is
        # self-contradictory.
        shown, limit = outcome.reasons[0].split("Turnover ")[1].split(" > ")
        assert shown != limit

    def test_none_disables_an_individual_rule(self):
        config = FilterConfig(
            min_sharpe=None, min_fitness=None, max_turnover=None,
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
        )
        result = make_result(sharpe=-5.0, fitness=-5.0, turnover=99.0)
        assert evaluate_filters(result, config).passed is True

    def test_missing_metric_cannot_pass(self):
        result = make_result(sharpe=None, fitness=None)
        outcome = evaluate_filters(result, DEFAULTS)
        assert outcome.passed is False
        assert "Sharpe missing - cannot verify >= 1.25" in outcome.reasons
        assert "Fitness missing - cannot verify >= 1.00" in outcome.reasons

    def test_optional_rules_on_returns_drawdown_margin(self):
        config = FilterConfig(
            min_sharpe=None, min_fitness=None, max_turnover=None,
            min_returns=0.10, max_drawdown=0.08, min_margin=0.0005,
            min_positive_year_ratio=None, max_negative_sharpe_years=None,
        )
        assert evaluate_filters(make_result(), config).passed is True
        bad = make_result(returns=0.05, drawdown=0.20, margin=0.0001)
        outcome = evaluate_filters(bad, config)
        assert outcome.passed is False
        assert len(outcome.reasons) == 3
        assert "Returns 5.0% < 10.0%" in outcome.reasons
        assert "Drawdown 20.0% > 8.0%" in outcome.reasons

    def test_threshold_filter_directly(self):
        reasons = ThresholdFilter(DEFAULTS).evaluate(make_result(sharpe=0.1))
        assert reasons == ["Sharpe 0.10 < 1.25"]


class TestNonCompletedNeverPasses:
    def test_failed_simulation_fails_with_status_reason(self):
        result = make_result(status=SimulationStatus.FAILED, error="unit handling mismatch")
        outcome = evaluate_filters(result, DEFAULTS)
        assert outcome.passed is False
        assert outcome.reasons == [
            f"Status {SimulationStatus.FAILED} != {SimulationStatus.COMPLETED} "
            "(unit handling mismatch)"
        ]

    def test_timeout_fails(self):
        outcome = evaluate_filters(make_result(status=SimulationStatus.TIMEOUT), DEFAULTS)
        assert outcome.passed is False
        assert "TIMEOUT" in outcome.reasons[0]

    def test_skipped_fails(self):
        outcome = evaluate_filters(make_result(status=SimulationStatus.SKIPPED), DEFAULTS)
        assert outcome.passed is False

    def test_metrics_are_ignored_when_not_completed(self):
        # Perfect numbers must not rescue a run that never finished.
        result = make_result(status=SimulationStatus.REQUEST_ERROR, sharpe=9.9, fitness=9.9)
        assert evaluate_filters(result, DEFAULTS).passed is False
