"""Failure-reason taxonomy.

"FAILED" is not one failure. An alpha with no raw signal (LOW_SHARPE) needs a
completely different next experiment from an alpha that is strong but collides
with an existing cluster (SELF_CORRELATION). Every rejected factor gets a stable
machine-readable code (plus optional secondary codes) so the generation context
can split *dead directions* from *redundant-but-strong ideas*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

#: Primary failure codes. Kept as plain string constants on purpose so they
#: store/serialize trivially.
BAD_SHARPE = "BAD_SHARPE"
BAD_FITNESS = "BAD_FITNESS"
HIGH_TURNOVER = "HIGH_TURNOVER"
LOW_TURNOVER = "LOW_TURNOVER"
ONE_SIDED_BOOK = "ONE_SIDED_BOOK"
SUB_UNIVERSE_FAIL = "SUB_UNIVERSE_FAIL"
SELF_CORRELATION = "SELF_CORRELATION"
COMPETITION_MATCH = "COMPETITION_MATCH"
CONCENTRATED_WEIGHT = "CONCENTRATED_WEIGHT"
OVERFIT = "OVERFIT"
SIMULATION_ERROR = "SIMULATION_ERROR"
DUPLICATE = "DUPLICATE"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

#: Human labels (also surfaced in the report).
FAILURE_LABELS: dict[str, str] = {
    BAD_SHARPE: "Weak standalone signal (sharpe below bar)",
    BAD_FITNESS: "Weak risk-adjusted quality (fitness below bar)",
    HIGH_TURNOVER: "Turns over too fast (cost / capacity)",
    LOW_TURNOVER: "Book barely trades — signal not in stock selection",
    ONE_SIDED_BOOK: "One-sided book (degenerate long/short profile)",
    SUB_UNIVERSE_FAIL: "Does not survive on sub-universes",
    SELF_CORRELATION: "Strong idea but redundant vs an existing alpha/cluster",
    COMPETITION_MATCH: "Collides with the competition-elicibility universe",
    CONCENTRATED_WEIGHT: "Piles into too few names (concentrated weight)",
    OVERFIT: "Great in train, collapses out of sample",
    SIMULATION_ERROR: "Simulation itself errored/timed out",
    DUPLICATE: "Exact experiment or saturated low-novelty variant",
    UNKNOWN_FAILURE: "Rejected without a classifiable reason",
}

#: BRAIN submission check name -> failure code.
_CHECK_map: dict[str, str] = {
    "LOW_SHARPE": BAD_SHARPE,
    "LOW_FITNESS": BAD_FITNESS,
    "LOW_TURNOVER": LOW_TURNOVER,
    "HIGH_TURNOVER": HIGH_TURNOVER,
    "LOW_SUB_UNIVERSE_SHARPE": SUB_UNIVERSE_FAIL,
    "SELF_CORRELATION": SELF_CORRELATION,
    "MATCHES_COMPETITION": COMPETITION_MATCH,
    "CONCENTRATED_WEIGHT": CONCENTRATED_WEIGHT,
}

_REASON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"self[_\s-]?corr", re.I), SELF_CORRELATION),
    (re.compile(r"sharpe", re.I), BAD_SHARPE),
    (re.compile(r"fitness", re.I), BAD_FITNESS),
    (re.compile(r"turnover", re.I), HIGH_TURNOVER),
    (re.compile(r"sub[_\s-]?universe", re.I), SUB_UNIVERSE_FAIL),
    (re.compile(r"competition", re.I), COMPETITION_MATCH),
    (re.compile(r"concentrated|weight", re.I), CONCENTRATED_WEIGHT),
    (re.compile(r"one[-\s]?sided|long-only|long only", re.I), ONE_SIDED_BOOK),
    (re.compile(r"duplicate|novelty", re.I), DUPLICATE),
)

#: Heuristic ordering for a primary code when several fired.
_PRIORITY = (
    SELF_CORRELATION, COMPETITION_MATCH, SUB_UNIVERSE_FAIL, ONE_SIDED_BOOK,
    CONCENTRATED_WEIGHT, LOW_TURNOVER, HIGH_TURNOVER, OVERFIT,
    BAD_SHARPE, BAD_FITNESS, DUPLICATE, SIMULATION_ERROR,
)


@dataclass(frozen=True)
class FailureAssessment:
    primary: str
    codes: tuple[str, ...]
    label: str
    one_sided: bool = False
    overfit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "codes": list(self.codes),
            "label": self.label,
            "one_sided_book": self.one_sided,
            "overfit": self.overfit,
        }


def _check_codes(checks: dict[str, Any] | None) -> list[str]:
    codes: list[str] = []
    for name, payload in (checks or {}).items():
        if not isinstance(payload, dict):
            continue
        result = str(payload.get("result") or "").upper()
        if result and result != "PASS":
            code = _CHECK_map.get(str(name).upper())
            if code and code not in codes:
                codes.append(code)
    return codes


def _reason_codes(reasons: Iterable[str] | None) -> list[str]:
    codes: list[str] = []
    for text in reasons or ():
        for pattern, code in _REASON_PATTERNS:
            if pattern.search(str(text)) and code not in codes:
                codes.append(code)
    return codes


def _is_one_sided(
    long_count: float | None,
    short_count: float | None,
    threshold: float,
) -> bool:
    if not long_count or not short_count:
        return False
    total = float(long_count) + float(short_count)
    if total <= 0:
        return False
    return abs(float(long_count) - float(short_count)) / total >= threshold


def _is_overfit(
    train_sharpe: float | None,
    test_sharpe: float | None,
    *,
    retention: float,
    min_train_sharpe: float,
) -> bool:
    if train_sharpe is None or test_sharpe is None:
        return False
    if float(train_sharpe) < min_train_sharpe:
        return False
    return float(test_sharpe) / float(train_sharpe) < retention


def classify_failure(
    *,
    status: str | None = None,
    checks: dict[str, Any] | None = None,
    reasons: Iterable[str] | None = None,
    sharpe: float | None = None,
    fitness: float | None = None,
    long_count: float | None = None,
    short_count: float | None = None,
    train_sharpe: float | None = None,
    test_sharpe: float | None = None,
    one_sided_ratio: float = 0.80,
    overfit_retention: float = 0.50,
    overfit_min_train_sharpe: float = 1.5,
) -> FailureAssessment:
    """Assign the most informative failure code.

    Status (CORR_REJECTED / SIMULATION_FAILED / DUPLICATE) wins when present;
    otherwise evidence is merged from BRAIN check results, textual filter
    reasons and book/test heuristics.
    """
    from .store import FactorStatus  # local import: no import cycle at module load

    codes: list[str] = []
    if status == FactorStatus.CORR_REJECTED:
        codes.append(SELF_CORRELATION)
    elif status == FactorStatus.SIMULATION_FAILED:
        codes.append(SIMULATION_ERROR)
    elif status == FactorStatus.DUPLICATE:
        codes.append(DUPLICATE)

    codes.extend(_check_codes(checks))
    codes.extend(_reason_codes(reasons))

    one_sided = _is_one_sided(long_count, short_count, one_sided_ratio)
    if one_sided and ONE_SIDED_BOOK not in codes:
        codes.append(ONE_SIDED_BOOK)

    overfit = _is_overfit(
        train_sharpe, test_sharpe,
        retention=overfit_retention,
        min_train_sharpe=overfit_min_train_sharpe,
    )
    if overfit and OVERFIT not in codes:
        codes.append(OVERFIT)

    if not codes:
        # Last-resort metric inference for a factor rejected without a
        # parsable reason/check payload.
        if sharpe is not None and float(sharpe) < 1.25:
            codes.append(BAD_SHARPE)
        elif fitness is not None and float(fitness) < 1.0:
            codes.append(BAD_FITNESS)
        else:
            codes.append(UNKNOWN_FAILURE)

    def rank(code: str) -> int:
        return _PRIORITY.index(code) if code in _PRIORITY else len(_PRIORITY)

    ordered = sorted(dict.fromkeys(codes), key=rank)
    primary = ordered[0]
    return FailureAssessment(
        primary=primary,
        codes=tuple(ordered),
        label=FAILURE_LABELS.get(primary, FAILURE_LABELS[UNKNOWN_FAILURE]),
        one_sided=one_sided,
        overfit=overfit,
    )
