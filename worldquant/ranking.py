"""Leaderboard ordering and rendering.

Default ordering, best first:
    1. Fitness      DESC
    2. Sharpe       DESC
    3. Turnover     ASC
    4. Drawdown     ASC

Missing metrics sort last: a ``None`` fitness is treated as ``-inf`` for the
descending keys and ``+inf`` for the ascending ones, so an incomplete run can
never outrank a measured one.
"""

from __future__ import annotations

import math
from typing import Sequence

from .api import SUBMISSION_CHECKS, SimulationStatus, passed_check_count
from .models import AlphaResult

DEFAULT_SORT_FIELDS: tuple[tuple[str, bool], ...] = (
    ("fitness", True),
    ("sharpe", True),
    ("turnover", False),
    ("drawdown", False),
)

_EXPRESSION_WIDTH = 42


def _build_key(result: AlphaResult, fields: Sequence[tuple[str, bool]]) -> tuple[float, ...]:
    """Build a sort key where smaller always means better.

    Descending fields are negated. A missing metric maps to ``+inf`` in both
    directions, so an unmeasured run always ranks below a measured one instead
    of accidentally floating to the top.
    """
    key: list[float] = []
    for field, descending in fields:
        value = getattr(result, field, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            key.append(math.inf)
            continue
        number = float(value)
        if math.isnan(number):
            key.append(math.inf)
        else:
            key.append(-number if descending else number)
    return tuple(key)


def latest_per_alpha(results: Sequence[AlphaResult]) -> list[AlphaResult]:
    """Collapse repeated runs of one alpha down to the most recent.

    ``--force`` and resumed timeouts legitimately leave several simulation rows
    for the same expression+settings. Ranking all of them lists one alpha twice,
    and the older row may predate a parser fix (so its yearly stats are empty
    while the newer one has them). Storage returns rows ordered by simulation id
    ascending, so the last occurrence wins.
    """
    latest: dict[str, AlphaResult] = {}
    for result in results:
        identity = result.dedup_key or result.expression
        latest[identity] = result
    return list(latest.values())


def rank_results(
    results: Sequence[AlphaResult],
    *,
    only_completed: bool = True,
    include_excluded: bool = False,
    sort_fields: Sequence[tuple[str, bool]] | None = None,
) -> list[AlphaResult]:
    """Return results ordered best-first.

    Args:
        only_completed: Drop runs that never produced metrics. Failed and
            timed-out simulations are still preserved in the CSV exports.
        include_excluded: Keep alphas carrying a permanent exclusion tag
            (``excluded_reason``). Defaults to False so the operator-facing
            leaderboard and any caller that picks "the best usable alphas"
            can never surface an alpha that was screened out for a structural
            reason (e.g. ``SELF_CORRELATION`` permanently over the limit).
            Set True only for diagnostic listings that need to show the
            whole pool.
        sort_fields: Override the default ordering with ``(attribute, descending)``
            pairs.
    """
    pool = list(results)
    if only_completed:
        pool = [item for item in pool if item.status == SimulationStatus.COMPLETED]
    if not include_excluded:
        pool = [item for item in pool if not item.is_excluded]

    fields = tuple(sort_fields) if sort_fields else DEFAULT_SORT_FIELDS
    return sorted(pool, key=lambda result: _build_key(result, fields))


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def year_stability_label(result: AlphaResult) -> str:
    """Compact yearly-stability summary, e.g. ``4/5+ s=0.31 w=-0.94``."""
    summary = result.yearly_summary
    if summary.is_empty:
        return "n/a"
    return (
        f"{summary.positive_years}/{summary.total_years}+ "
        f"s={_fmt(summary.yearly_sharpe_std)} "
        f"w={_fmt(summary.worst_year_sharpe)}"
    )


def format_leaderboard(results: Sequence[AlphaResult], *, top: int = 20) -> str:
    """Render the ranked table as fixed-width text for console and log output.

    ``Grade`` is BRAIN's own verdict (INFERIOR / AVERAGE / GOOD), placed early
    because it is the number that actually answers "is this alpha any good" —
    the surrounding metrics are only the inputs to it.

    ``Subm`` is the submission verdict (``n/8``, or ``-`` when the check never
    ran). It has to be visible here because ranking follows fitness, and neither
    fitness nor grade says anything about submittability: a table of the best
    alphas can be headed by nine that cannot actually be submitted, with the one
    usable alpha sitting below them.
    """
    ranked = rank_results(results)[: max(0, top)]
    if not ranked:
        return "No completed alphas to rank."

    header = (
        f"{'Rank':>4}  {'Alpha ID':<18} {'Grade':<9} {'Subm':>4} "
        f"{'Expression':<{_EXPRESSION_WIDTH}} "
        f"{'Sharpe':>7} {'Fitness':>8} {'Turnover':>9} {'Returns':>8} "
        f"{'Drawdown':>9} {'Margin':>8}  {'Year Stability':<22}"
    )
    lines = [f"Top {len(ranked)} Alpha", header, "-" * len(header)]

    for index, result in enumerate(ranked, start=1):
        expression = result.expression.replace("\n", " ")
        if len(expression) > _EXPRESSION_WIDTH:
            # ASCII ellipsis: the Windows console default codepage mangles "…".
            expression = expression[: _EXPRESSION_WIDTH - 3] + "..."
        alpha_id = (result.alpha_id or "")[:18]
        grade = (result.grade or "-")[:9]
        subm = (
            "-" if not result.submission_checks
            else f"{passed_check_count(result.submission_checks)}/{len(SUBMISSION_CHECKS)}"
        )
        lines.append(
            f"{index:>4}  {alpha_id:<18} {grade:<9} {subm:>4} "
            f"{expression:<{_EXPRESSION_WIDTH}} "
            f"{_fmt(result.sharpe):>7} {_fmt(result.fitness):>8} {_pct(result.turnover):>9} "
            f"{_pct(result.returns):>8} {_pct(result.drawdown):>9} "
            f"{_fmt(result.margin, 4):>8}  {year_stability_label(result):<22}"
        )

    return "\n".join(lines)
