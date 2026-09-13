"""Internal data models.

These dataclasses are the contract between the HTTP layer, the storage layer and
the filters. Everything BRAIN-specific is parsed once in :mod:`worldquant.api`
and lands here as plain Python values, so downstream code never touches raw JSON.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any

from .api import (
    SUBMISSION_CHECKS,
    SimulationStatus,
    all_checks_passed,
    failed_check_reasons,
    passed_check_count,
)


@dataclass
class AlphaSpec:
    """One alpha to run: a name, an expression, optional per-alpha settings.

    The four optional metadata fields carry ablation provenance. They are
    read by the Factor Registry pre-simulation gate (via attribute access) and
    ignored everywhere else, so ordinary specs behave exactly as before:

    * ``source`` — ``"ablation"`` marks an explicit parameter sweep variant;
      such specs are exempt from the same-signal capacity/novelty blocks but
      exact experiment duplicates are still rejected (unless forced).
    * ``ablation_group_id`` — sweep id shared by every variant in one study.
    * ``changed_parameters`` — ``{setting: new_value}`` diff vs the parent.
    * ``parent_experiment_id`` — registry factor id of the baseline experiment.
    """

    expression: str
    name: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    ablation_group_id: str | None = None
    changed_parameters: dict[str, Any] | None = None
    parent_experiment_id: int | None = None

    @property
    def label(self) -> str:
        """Display identifier: explicit name when given, otherwise the hash prefix."""
        return self.name or self.expression[:40]

    @property
    def is_ablation(self) -> bool:
        return self.source == "ablation"


@dataclass
class YearlySummary:
    """Stability statistics derived from per-year backtest numbers."""

    positive_years: int = 0
    negative_years: int = 0
    neutral_years: int = 0
    total_years: int = 0
    positive_year_ratio: float | None = None
    yearly_sharpe_std: float | None = None
    worst_year_sharpe: float | None = None
    best_year_sharpe: float | None = None

    @property
    def is_empty(self) -> bool:
        return self.total_years == 0

    def as_flat_dict(self) -> dict[str, Any]:
        return {
            "positive_years": self.positive_years,
            "negative_years": self.negative_years,
            "neutral_years": self.neutral_years,
            "total_years": self.total_years,
            "positive_year_ratio": self.positive_year_ratio,
            "yearly_sharpe_std": self.yearly_sharpe_std,
            "worst_year_sharpe": self.worst_year_sharpe,
            "best_year_sharpe": self.best_year_sharpe,
        }


def summarize_yearly(yearly_stats: dict[str, dict[str, float | None]] | None) -> YearlySummary:
    """Compute stability statistics from ``{year: {metric: value}}``.

    Years whose sharpe is missing are excluded from every statistic rather than
    being counted as zero, so a partial payload never skews the ratio.
    """
    summary = YearlySummary()
    if not yearly_stats:
        return summary

    sharpes: list[float] = []
    for metrics in yearly_stats.values():
        if not isinstance(metrics, dict):
            continue
        sharpe = metrics.get("sharpe")
        if not isinstance(sharpe, (int, float)) or isinstance(sharpe, bool):
            continue
        sharpes.append(float(sharpe))

    summary.total_years = len(sharpes)
    if not sharpes:
        return summary

    summary.positive_years = sum(1 for s in sharpes if s > 0)
    summary.negative_years = sum(1 for s in sharpes if s < 0)
    summary.neutral_years = summary.total_years - summary.positive_years - summary.negative_years
    summary.positive_year_ratio = summary.positive_years / summary.total_years
    summary.yearly_sharpe_std = statistics.stdev(sharpes) if len(sharpes) >= 2 else 0.0
    summary.worst_year_sharpe = min(sharpes)
    summary.best_year_sharpe = max(sharpes)
    return summary


@dataclass
class AlphaResult:
    """Everything we know about one alpha run.

    ``alpha_id`` is the *local* identifier (CSV ``name`` or a hash of the
    expression). ``remote_alpha_id`` is BRAIN's own alpha id, filled in only once
    a simulation completes.

    Turnover / returns / drawdown / margin are decimal fractions exactly as
    BRAIN returns them (0.423 == 42.3%). Never store a percent here.
    """

    alpha_id: str
    expression: str
    dedup_key: str
    status: str = SimulationStatus.PENDING

    simulation_id: str | None = None
    remote_alpha_id: str | None = None
    settings_json: str = "{}"

    sharpe: float | None = None
    fitness: float | None = None
    turnover: float | None = None
    returns: float | None = None
    drawdown: float | None = None
    margin: float | None = None
    pnl: float | None = None
    book_size: float | None = None
    long_count: int | None = None
    short_count: int | None = None

    yearly_stats: dict[str, dict[str, float | None]] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    raw_json: str | None = None

    #: BRAIN's own verdict on the alpha (e.g. INFERIOR / AVERAGE / GOOD).
    #: Independent of this project's configurable pass/fail filters.
    grade: str | None = None
    #: In-sample vs out-of-sample stage reported by BRAIN, usually "IS".
    stage: str | None = None
    #: Parsed ``train`` / ``test`` blocks, present only when the simulation ran
    #: with a ``testPeriod``. The held-out year is the point of that setting, so
    #: it is kept rather than discarded.
    train_stats: dict[str, Any] | None = None
    test_stats: dict[str, Any] | None = None
    #: The eight submission checks from ``GET /alphas/{id}/check``. This is the
    #: only place ``SELF_CORRELATION`` is ever resolved — the plain alpha payload
    #: leaves it ``PENDING`` indefinitely.
    submission_checks: dict[str, Any] | None = None
    #: Worst correlation against the account's existing alphas, from that check.
    self_correlation: float | None = None
    #: Per-neighbor rows behind ``self_correlation`` (each with at least
    #: ``alpha_id`` and ``correlation``), when the check payload exposes them.
    #: Feeds the registry's pairwise correlation graph; never re-requested.
    self_correlated_with: list[dict[str, Any]] | None = None

    created_at: str = ""
    completed_at: str | None = None
    error: str | None = None

    passed: bool | None = None
    reasons: list[str] = field(default_factory=list)

    #: Permanent exclusion tag set by `ResultStore.mark_excluded` (e.g.
    #: "SELF_CORRELATION_BLOCKED"). Once set, the alpha is filtered out of
    #: every "usable alpha" path (`rank_results`, the search's
    #: `--include-existing` pool) so the operator never re-selects a
    #: structurally-rejected alpha from the leaderboard.
    excluded_reason: str | None = None

    @property
    def is_excluded(self) -> bool:
        """True when this alpha carries a permanent exclusion tag."""
        return bool(self.excluded_reason)

    @property
    def yearly_summary(self) -> YearlySummary:
        return summarize_yearly(self.yearly_stats)

    @property
    def is_terminal(self) -> bool:
        return self.status in SimulationStatus.TERMINAL

    @property
    def is_success(self) -> bool:
        return self.status == SimulationStatus.COMPLETED

    @property
    def is_one_sided_book(self) -> bool:
        """True when the portfolio is entirely long or entirely short.

        A one-sided book is not a market-neutral alpha: its returns and fitness
        come from directional concentration, not from cross-sectional signal.
        BRAIN still grades it on fitness alone, so this can score GOOD while being
        meaningless as an alpha — observed live with shortCount 0, returns 41.6%
        and drawdown 53.3%. Worth flagging wherever the grade is reported.

        Unknown counts return False, so an incomplete run is never falsely
        flagged.
        """
        if self.long_count is None or self.short_count is None:
            return False
        return self.long_count == 0 or self.short_count == 0

    @property
    def is_submittable(self) -> bool:
        """True only when all eight BRAIN submission checks read PASS.

        The grade is **not** evidence of submittability: it follows fitness alone,
        and alphas graded GOOD have been observed carrying two or three failing
        checks plus a long-only book. ``False`` also covers "check never ran" —
        an unverified alpha is not a submittable one.
        """
        return all_checks_passed(self.submission_checks)

    @property
    def submission_failures(self) -> list[str]:
        """Human-readable reasons for each submission check that did not pass."""
        return failed_check_reasons(self.submission_checks)

    @property
    def test_sharpe(self) -> float | None:
        """Sharpe over the held-out test year, when a testPeriod was used."""
        if not isinstance(self.test_stats, dict):
            return None
        value = self.test_stats.get("sharpe")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def overfit_ratio(self) -> float | None:
        """Test-year sharpe as a fraction of the in-sample sharpe.

        This is the signal the mandatory one-year test period exists to expose.
        BRAIN grades on the full IS window, so an alpha can grade well while its
        held-out year collapses: observed live at IS sharpe 1.80 (train 2.05)
        against a test sharpe of 0.46 — graded GOOD, ratio 0.26.

        ``None`` when either number is missing, or when the IS sharpe is not
        positive, since a ratio against a negative baseline is meaningless.
        """
        test = self.test_sharpe
        if test is None or self.sharpe is None or self.sharpe <= 0:
            return None
        return test / self.sharpe

    def metrics_line(self) -> str:
        """One-line metric summary for the log, turnover rendered as a percent."""
        def fmt(value: float | None, digits: int = 2) -> str:
            return "n/a" if value is None else f"{value:.{digits}f}"

        def pct(value: float | None) -> str:
            return "n/a" if value is None else f"{value * 100:.1f}%"

        line = (
            f"Sharpe={fmt(self.sharpe)} Fitness={fmt(self.fitness)} "
            f"Turnover={pct(self.turnover)} Returns={pct(self.returns)} "
            f"Drawdown={pct(self.drawdown)} Margin={fmt(self.margin, 4)} "
            f"Grade={self.grade or 'n/a'}"
        )
        if self.test_sharpe is not None:
            # The grade covers the full IS window; the held-out year is the
            # number that says whether it will survive.
            line += f" TestSharpe={fmt(self.test_sharpe)}"
        return line

    def to_csv_row(self) -> dict[str, Any]:
        """Flat dict for CSV export; nested values become JSON strings."""
        summary = self.yearly_summary
        row: dict[str, Any] = {
            "alpha_id": self.alpha_id,
            "expression": self.expression,
            "dedup_key": self.dedup_key,
            "status": self.status,
            "grade": self.grade or "",
            "stage": self.stage or "",
            "simulation_id": self.simulation_id or "",
            "remote_alpha_id": self.remote_alpha_id or "",
            "sharpe": self.sharpe,
            "fitness": self.fitness,
            "turnover": self.turnover,
            "returns": self.returns,
            "drawdown": self.drawdown,
            "margin": self.margin,
            "pnl": self.pnl,
            "book_size": self.book_size,
            "long_count": self.long_count,
            "short_count": self.short_count,
            "positive_years": summary.positive_years,
            "negative_years": summary.negative_years,
            "total_years": summary.total_years,
            "positive_year_ratio": summary.positive_year_ratio,
            "yearly_sharpe_std": summary.yearly_sharpe_std,
            "worst_year_sharpe": summary.worst_year_sharpe,
            "passed": "" if self.passed is None else bool(self.passed),
            # The submission verdict sits next to `passed` on purpose: they answer
            # different questions, and a factor can clear every configured
            # threshold while still being unsubmittable.
            "submittable": (
                "" if not self.submission_checks
                else ("YES" if self.is_submittable else "NO")
            ),
            "checks_passed": (
                "" if not self.submission_checks
                else f"{passed_check_count(self.submission_checks)}/{len(SUBMISSION_CHECKS)}"
            ),
            "checks_failed": ", ".join(self.submission_failures),
            "self_correlation": self.self_correlation,
            "reasons": "; ".join(self.reasons),
            "created_at": self.created_at,
            "completed_at": self.completed_at or "",
            "error": self.error or "",
            "settings_json": self.settings_json,
            "yearly_stats_json": json.dumps(self.yearly_stats, sort_keys=True) if self.yearly_stats else "",
        }
        return row


@dataclass
class FilterOutcome:
    """Result of running the filter chain over one alpha."""

    passed: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed
