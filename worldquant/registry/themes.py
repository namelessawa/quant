"""Rule-based research theme / branch taxonomy.

A theme is a three-level, deterministic research branch such as
``OPTION.implied_volatility.skew`` or ``ANALYST.earnings``. Its only purpose is to
answer "which direction has been tested a lot / barely at all" — it is NOT a
finance encyclopedia and never uses an LLM; every branch comes from literal
field-name/dataset tokens.

    factor family  (ANALYST)
        └─ branch      (earnings)
              └─ sub-branch, when the data clearly supports one (skew)
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from .families import (
    ANALYST,
    FUNDAMENTAL,
    LIQUIDITY,
    MODEL,
    NEWS,
    OPTION,
    PRICE,
    RISK,
    SENTIMENT,
    FieldFamilyResolver,
)

# --------------------------------------------------------------------------- #
# Branch rules: (regex, branch[, subbranch]). First match per family wins.
# --------------------------------------------------------------------------- #
_ANALYST_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"recommend|target_price|rating", re.I), "recommendation", None),
    (re.compile(r"revenue|sales_", re.I), "revenue", None),
    (re.compile(r"bvps|book_value", re.I), "book_value", None),
    (re.compile(r"estimate|est_|_est|consensus|revision", re.I), "earnings_estimates", None),
    (re.compile(r"ebitda", re.I), "earnings", "ebitda"),
    (re.compile(r"ebit", re.I), "earnings", "ebit"),
    (re.compile(r"operating_income|op_income", re.I), "earnings", "operating_income"),
    (re.compile(r"netprofit|net_income|netincome|net_profit", re.I), "earnings", "net_profit"),
    (re.compile(r"grossincome|gross_profit|gross_margin", re.I), "earnings", "gross_profit"),
    (re.compile(r"earning|eps|profit", re.I), "earnings", None),
)

_OPTION_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"skew", re.I), "implied_volatility", "skew"),
    (re.compile(r"pcr|put_call|call_put|oi_", re.I), "put_call_ratio", None),
    (re.compile(r"term|_10\b|_60\b|_90\b|_180\b|_360\b", re.I),
     "implied_volatility", "term_structure"),
    (re.compile(r"implied_volatility|impl_vol|iv_|\biv\d|^iv", re.I),
     "implied_volatility", None),
    (re.compile(r"surface|strike|moneyness", re.I), "vol_surface", None),
    (re.compile(r"call_|put_|option_volume", re.I), "options_flow", None),
)

_MODEL_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"rel_ret|rel_num|relationship|competitor|spillover", re.I),
     "relationship_graph", None),
    (re.compile(r"rank_derivative|derivative", re.I), "rank_derivative", None),
    (re.compile(r"systematic_risk|systerm|beta|factor_exposure", re.I),
     "systematic_risk", None),
)

_NEWS_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"qep|earnings_event", re.I), "event", "earnings_event"),
    (re.compile(r"nws|news", re.I), "event", None),
)

_SENTIMENT_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"buzz", re.I), "buzz", None),
    (re.compile(r"sentiment|snt|socialmedia|smedia", re.I), "sentiment", None),
)

_FUNDAMENTAL_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"fcf|free_cash|cfo|cash_flow|operating_cash", re.I), "cashflow", None),
    (re.compile(r"debt|leverage|liabilit", re.I), "leverage", None),
    (re.compile(r"operating_income|op_income|ebit|earnings|income|profit", re.I),
     "earnings", None),
    (re.compile(r"revenue|sales", re.I), "revenue", None),
    (re.compile(r"asset|book_value|equity", re.I), "assets", None),
)

_LIQUIDITY_RULES: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"cap$|mkt_cap|market_cap|sharesout", re.I), "size", None),
    (re.compile(r"volume|adv|dvol|turnover", re.I), "volume", None),
)

_REVERSAL_OPS = frozenset(
    {"ts_zscore", "ts_rank", "ts_scale", "zscore", "rank", "group_rank",
     "group_zscore", "ts_av_diff", "ts_regression", "normalize"}
)
_MOMENTUM_OPS = frozenset(
    {"ts_delta", "ts_returns", "ts_decay_linear", "decay_linear", "ts_mean",
     "ts_sum", "mom"}
)


def _match(
    rules: tuple[tuple[re.Pattern[str], str, str | None], ...], name: str
) -> tuple[str, str | None] | None:
    for pattern, branch, subbranch in rules:
        if pattern.search(name):
            return branch, subbranch
    return None


def field_theme(field_name: str, resolver: FieldFamilyResolver) -> str | None:
    """Theme path contributed by one concrete field, e.g. ``OPTION.implied_volatility.skew``."""
    family = resolver(field_name)
    name = field_name.strip()
    rules_for = {
        ANALYST: _ANALYST_RULES,
        OPTION: _OPTION_RULES,
        MODEL: _MODEL_RULES,
        NEWS: _NEWS_RULES,
        SENTIMENT: _SENTIMENT_RULES,
        FUNDAMENTAL: _FUNDAMENTAL_RULES,
        LIQUIDITY: _LIQUIDITY_RULES,
    }.get(family)
    branch: str
    subbranch: str | None
    if rules_for:
        match = _match(rules_for, name)
        if match:
            branch, subbranch = match
        else:
            branch, subbranch = "other", None
    elif family == PRICE:
        branch, subbranch = "price", None
    elif family == RISK:
        branch, subbranch = "risk", None
    else:
        return None
    path = f"{family}.{branch}"
    if subbranch:
        path += f".{subbranch}"
    return path


def classify_theme(
    fields: Iterable[str],
    operators: Iterable[str] | None = None,
    *,
    resolver: FieldFamilyResolver | None = None,
) -> str | None:
    """Factor-level theme: the dominant field theme, with price fields refined
    by operator semantics (reversal vs momentum)."""
    resolver = resolver or FieldFamilyResolver()
    field_list = list(fields)
    if not field_list:
        return None
    themes = [path for path in (field_theme(f, resolver) for f in field_list) if path]
    if not themes:
        return None

    # Dominant top-level family first, so one price field inside an analyst
    # expression cannot steal the theme.
    families = Counter(path.split(".", 1)[0] for path in themes)
    dominant_family, _ = families.most_common(1)[0]
    in_family = [path for path in themes if path.startswith(dominant_family + ".")]
    branches = Counter(".".join(path.split(".")[:2]) for path in in_family)
    theme, _ = branches.most_common(1)[0]

    if dominant_family == PRICE:
        op_set = {str(op).lower() for op in (operators or ())}
        if op_set & _REVERSAL_OPS:
            theme = "PRICE.reversal"
        elif op_set & _MOMENTUM_OPS:
            theme = "PRICE.momentum"
    return theme
