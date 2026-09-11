"""Configurable pass/fail filters for backtested alphas.

Two independent rule groups are evaluated:

* aggregate thresholds (sharpe / fitness / turnover / ...), and
* yearly-stability rules, which catch alphas whose headline Sharpe looks fine
  but whose year-by-year record is erratic.

All ratio-like inputs and thresholds are decimal fractions (0.70 == 70%),
matching how BRAIN reports turnover/returns/drawdown. Percentages appear only in
human-readable reason strings.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

from .api import SimulationStatus
from .config import FilterConfig
from .logging_utils import get_logger
from .models import AlphaResult, FilterOutcome


def _fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


#: How many extra decimals :func:`_render_pair` may add to keep a violation
#: message from rendering the value and its limit identically.
_MAX_EXTRA_DIGITS = 4

#: Thresholds arrive as decimal literals from a config file while metrics arrive
#: as binary floats from JSON, so an alpha sitting exactly on a limit can miss it
#: by a few ULP (turnover 0.7000000000000001 vs a 0.70 cap). Comparisons treat
#: that as satisfying the limit; only a real difference counts as a violation.
_REL_TOL = 1e-9
_ABS_TOL = 1e-12


def _violates_below(value: float, limit: float) -> bool:
    """True when ``value`` is meaningfully below ``limit``."""
    return value < limit and not math.isclose(value, limit, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def _violates_above(value: float, limit: float) -> bool:
    """True when ``value`` is meaningfully above ``limit``."""
    return value > limit and not math.isclose(value, limit, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def _render(value: float | None, digits: int, percent: bool) -> str:
    """Render one number for a reason message.

    Percent rendering always starts at one decimal: ``digits`` is tuned for the
    decimal-fraction form, and multiplying by 100 shifts two places of it into
    the integer part, so reusing it would print ``5.0000%`` instead of ``5.0%``.
    """
    if value is None:
        return "n/a"
    return _pct(value, 1) if percent else _fmt(value, digits)


def _render_pair(
    value: float,
    limit: float,
    digits: int,
    *,
    percent: bool,
) -> tuple[str, str]:
    """Render a violating value and its limit so they never look identical.

    A genuine violation can still round to the same text as its limit at the
    base precision (0.7004 vs 0.70 both print as ``70.0%``), which would read as
    the self-contradictory ``70.0% > 70.0%``. Precision is raised until the two
    differ, up to :data:`_MAX_EXTRA_DIGITS` extra decimals.
    """
    base = 1 if percent else digits
    render = _pct if percent else _fmt
    shown = render(value, base)
    limit_shown = render(limit, base)
    for extra in range(1, _MAX_EXTRA_DIGITS + 1):
        if shown != limit_shown:
            return shown, limit_shown
        shown = render(value, base + extra)
        limit_shown = render(limit, base + extra)
    return shown, limit_shown


class Rule(Protocol):
    """A rule returns one human-readable reason per violation."""

    def evaluate(self, result: AlphaResult) -> list[str]: ...


class ThresholdFilter:
    """Aggregate-metric thresholds. ``None`` disables an individual rule."""

    def __init__(self, config: FilterConfig) -> None:
        self.config = config

    def evaluate(self, result: AlphaResult) -> list[str]:
        reasons: list[str] = []
        cfg = self.config

        reasons += self._at_least("Sharpe", result.sharpe, cfg.min_sharpe, digits=2)
        reasons += self._at_least("Fitness", result.fitness, cfg.min_fitness, digits=2)
        reasons += self._at_least("Returns", result.returns, cfg.min_returns, digits=4, percent=True)
        reasons += self._at_least("Margin", result.margin, cfg.min_margin, digits=4)
        reasons += self._at_most("Turnover", result.turnover, cfg.max_turnover, digits=1, percent=True)
        reasons += self._at_most("Drawdown", result.drawdown, cfg.max_drawdown, digits=1, percent=True)
        return reasons

    @staticmethod
    def _at_least(
        label: str,
        value: float | None,
        minimum: float | None,
        *,
        digits: int,
        percent: bool = False,
    ) -> list[str]:
        if minimum is None:
            return []
        if value is None:
            return [f"{label} missing - cannot verify >= {_render(minimum, digits, percent)}"]
        if _violates_below(value, minimum):
            shown, limit_shown = _render_pair(value, minimum, digits, percent=percent)
            return [f"{label} {shown} < {limit_shown}"]
        return []

    @staticmethod
    def _at_most(
        label: str,
        value: float | None,
        maximum: float | None,
        *,
        digits: int,
        percent: bool = False,
    ) -> list[str]:
        if maximum is None:
            return []
        if value is None:
            return [f"{label} missing - cannot verify <= {_render(maximum, digits, percent)}"]
        if _violates_above(value, maximum):
            shown, limit_shown = _render_pair(value, maximum, digits, percent=percent)
            return [f"{label} {shown} > {limit_shown}"]
        return []


class YearlyStabilityFilter:
    """Per-year consistency rules built on :class:`~worldquant.models.YearlySummary`.

    When no yearly data is available the rules are skipped, unless
    ``require_yearly_data`` is set. This keeps the batch usable even though the
    yearly-stats endpoint is unverified (see :mod:`worldquant.api`).
    """

    def __init__(self, config: FilterConfig, *, logger: Any = None) -> None:
        self.config = config
        self.log = logger or get_logger("filters")

    @property
    def enabled(self) -> bool:
        cfg = self.config
        return any(
            value is not None
            for value in (
                cfg.min_positive_year_ratio,
                cfg.max_negative_sharpe_years,
                cfg.min_worst_year_sharpe,
                cfg.max_yearly_sharpe_std,
            )
        ) or cfg.require_yearly_data

    def evaluate(self, result: AlphaResult) -> list[str]:
        if not self.enabled:
            return []

        summary = result.yearly_summary
        if summary.is_empty:
            if self.config.require_yearly_data:
                return ["Yearly data missing but filters.require_yearly_data is enabled"]
            self.log.debug(
                "no yearly data for %s; skipping yearly-stability rules", result.alpha_id
            )
            return []

        reasons: list[str] = []
        cfg = self.config

        if cfg.min_positive_year_ratio is not None and summary.positive_year_ratio is not None:
            if _violates_below(summary.positive_year_ratio, cfg.min_positive_year_ratio):
                reasons.append(
                    f"Positive years {summary.positive_years}/{summary.total_years} "
                    f"= {_pct(summary.positive_year_ratio)} < {_pct(cfg.min_positive_year_ratio)}"
                )

        if cfg.max_negative_sharpe_years is not None:
            if summary.negative_years > cfg.max_negative_sharpe_years:
                reasons.append(
                    f"Negative-sharpe years {summary.negative_years} > {cfg.max_negative_sharpe_years}"
                )

        if cfg.min_worst_year_sharpe is not None and summary.worst_year_sharpe is not None:
            if _violates_below(summary.worst_year_sharpe, cfg.min_worst_year_sharpe):
                reasons.append(
                    f"Worst year sharpe {_fmt(summary.worst_year_sharpe)} < "
                    f"{_fmt(cfg.min_worst_year_sharpe)}"
                )

        if cfg.max_yearly_sharpe_std is not None and summary.yearly_sharpe_std is not None:
            if _violates_above(summary.yearly_sharpe_std, cfg.max_yearly_sharpe_std):
                reasons.append(
                    f"Yearly sharpe std {_fmt(summary.yearly_sharpe_std)} > "
                    f"{_fmt(cfg.max_yearly_sharpe_std)}"
                )

        return reasons


def evaluate_filters(
    result: AlphaResult,
    config: FilterConfig,
    *,
    logger: Any = None,
) -> FilterOutcome:
    """Run every rule group over one result.

    An alpha that never completed cannot pass, regardless of thresholds — its
    metrics are absent, and treating that as a pass would silently promote
    broken runs.
    """
    log = logger or get_logger("filters")

    if result.status != SimulationStatus.COMPLETED:
        reason = f"Status {result.status} != {SimulationStatus.COMPLETED}"
        if result.error:
            reason += f" ({result.error})"
        outcome = FilterOutcome(passed=False, reasons=[reason])
        log.debug("filter outcome for %s: %s", result.alpha_id, outcome.reasons)
        return outcome

    rules: list[Rule] = [ThresholdFilter(config), YearlyStabilityFilter(config, logger=log)]
    reasons: list[str] = []
    for rule in rules:
        reasons.extend(rule.evaluate(result))

    outcome = FilterOutcome(passed=not reasons, reasons=reasons)
    return outcome
