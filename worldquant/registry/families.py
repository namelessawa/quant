"""Extensible field -> family mapping and rule-based factor family classification.

The mapping is intentionally three-level and data-driven:

    field  ->  dataset  ->  field_family

``close``/``vwap``/... resolve by name, while account-specific fields such as
``anl4_ebit_value`` resolve through the dataset id cached in
``data/field_catalog.json`` (``anl4_ebit_value -> analyst4 -> ANALYST``). New
datasets only need a dataset rule; unknown fields degrade to ``OTHER`` rather
than being force-classified.

Factor families are a *coarse research statistic*, derived with deterministic
rules from field families and operators — no LLM, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Field families
# --------------------------------------------------------------------------- #
PRICE = "PRICE"
LIQUIDITY = "LIQUIDITY"
FUNDAMENTAL = "FUNDAMENTAL"
ANALYST = "ANALYST"
OPTION = "OPTION"
NEWS = "NEWS"
SENTIMENT = "SENTIMENT"
MODEL = "MODEL"
RISK = "RISK"
OTHER = "OTHER"

#: Factor families (coarser than field families; exposed in reports/CLI).
PRICE_MOMENTUM = "PRICE_MOMENTUM"
PRICE_REVERSAL = "PRICE_REVERSAL"
VOLATILITY = "VOLATILITY"
FACTOR_FAMILIES = (
    PRICE_MOMENTUM,
    PRICE_REVERSAL,
    VOLATILITY,
    OPTION,
    LIQUIDITY,
    FUNDAMENTAL,
    ANALYST,
    NEWS,
    SENTIMENT,
    MODEL,
    RISK,
    OTHER,
    "UNKNOWN",
)

#: Exact price/liquidity fields shipped with every BRAIN universe.
_PRICE_FIELDS = frozenset(
    {"close", "open", "high", "low", "vwap", "price", "mid", "midprice", "returns"}
)
_LIQUIDITY_FIELDS = frozenset(
    {
        "volume", "turnover", "adv20", "adv30", "adv60", "adv120", "sharesout",
        "sharesout1", "cap", "market_cap",
    }
)

#: (regex, family) prefix/contains rules applied before dataset resolution.
_FIELD_NAME_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(close|open|high|low|vwap|price|returns|reflexive|fnd28_)", re.I), PRICE),
    (re.compile(r"^(volume|turnover|adv\d+|sharesout|dvol|mkt_cap|cap$)", re.I), LIQUIDITY),
    (re.compile(r"^(option|opt\d|impl_vol|iv_|iv\d|skew|surface|call_|put_)", re.I), OPTION),
    (re.compile(r"^(news|nws|news_|sentiment|snt|buzz|media_)", re.I), NEWS),
    (re.compile(r"^(anl\d|analyst|est_|estimate|recommend|target_price|consensus)", re.I), ANALYST),
    (re.compile(r"^(model\d|mdl|alpha_terminal|rel_ret)", re.I), MODEL),
)

#: Dataset-id (substring) -> field family. Extensible via
#: :meth:`FieldFamilyResolver.register_dataset`.
_DATASET_RULES: dict[str, str] = {
    "analyst": ANALYST,
    "model": MODEL,
    "fundamental": FUNDAMENTAL,
    "pv": PRICE,  # refined by name rules for volume-like fields
    "price": PRICE,
    "option": OPTION,
    "news": NEWS,
    "sentiment": SENTIMENT,
    "risk": RISK,
    "equity": FUNDAMENTAL,
}

#: Operators that express deviation-from-norm (reversal) vs continuation.
_REVERSAL_OPERATORS = frozenset(
    {
        "ts_zscore", "ts_rank", "ts_scale", "zscore", "rank", "group_rank",
        "group_zscore", "ts_av_diff", "ts_regression", "normalize",
    }
)
_MOMENTUM_OPERATORS = frozenset(
    {"ts_delta", "ts_returns", "ts_decay_linear", "decay_linear", "ts_mean", "ts_sum", "mom"}
)
_VOLATILITY_OPERATORS = frozenset(
    {
        "ts_std_dev", "std_dev", "ts_skewness", "ts_kurtosis", "ts_corr",
        "ts_covariance", "covariance", "corr", "ts_regression", "variance",
        "high", "low",
    }
)


@dataclass
class FieldFamilyResolver:
    """Resolve a field id to a family via name rules + dataset metadata.

    Args:
        field_datasets: optional ``{field_id: dataset_id}`` mapping (the
            project's ``data/field_catalog.json`` cache).
        extra_dataset_rules: dataset-id substring -> family, merged on top of
            the built-ins so new datasets never require code changes here.
    """

    field_datasets: dict[str, str] = field(default_factory=dict)
    extra_dataset_rules: dict[str, str] = field(default_factory=dict)

    def register_dataset(self, dataset_id: str, family: str) -> None:
        self.extra_dataset_rules[dataset_id.lower()] = family

    def register_field(self, field_id: str, dataset_id: str) -> None:
        self.field_datasets[field_id] = dataset_id

    def dataset_for(self, field_id: str) -> str | None:
        return self.field_datasets.get(field_id)

    def family_of_dataset(self, dataset_id: str | None) -> str | None:
        if not dataset_id:
            return None
        lowered = dataset_id.lower()
        for needle, family in self.extra_dataset_rules.items():
            if needle in lowered:
                return family
        for needle, family in _DATASET_RULES.items():
            if needle in lowered:
                return family
        return None

    def __call__(self, field_id: str) -> str:
        """Return the field family; ``OTHER`` when nothing matches."""
        token = field_id.strip()
        lowered = token.lower()
        if lowered in _PRICE_FIELDS:
            return PRICE
        if lowered in _LIQUIDITY_FIELDS:
            return LIQUIDITY
        for pattern, family in _FIELD_NAME_RULES:
            if pattern.search(token):
                # pv datasets refine volume-like names to LIQUIDITY even when a
                # price-prefix rule matched first.
                if family == PRICE and re.search(r"volume|adv\d|turnover", lowered):
                    return LIQUIDITY
                return family
        family = self.family_of_dataset(self.field_datasets.get(token))
        return family or OTHER


# --------------------------------------------------------------------------- #
# Factor family classification
# --------------------------------------------------------------------------- #
def classify_factor_family(
    fields: Iterable[str],
    operators: Iterable[str],
    *,
    resolver: FieldFamilyResolver | None = None,
) -> str:
    """Coarse factor family from the field families and operator bag.

    Priority is conservative: specific alternative-data families win over pure
    price/volume inference, and a price/volume expression is only labelled
    momentum/reversal when the operator evidence supports it. Returns
    ``UNKNOWN`` when the signal is too mixed to label reliably.
    """
    resolver = resolver or FieldFamilyResolver()
    field_list = list(fields)
    operator_set = {op.lower() for op in operators}
    families = {resolver(field) for field in field_list}

    if NEWS in families:
        return NEWS
    if SENTIMENT in families:
        return SENTIMENT
    if OPTION in families:
        return OPTION
    if RISK in families:
        return RISK
    if ANALYST in families:
        return ANALYST
    if MODEL in families:
        return MODEL
    if FUNDAMENTAL in families:
        return FUNDAMENTAL

    pv_families = {PRICE, LIQUIDITY}
    if not families or not families <= pv_families | {OTHER}:
        return "UNKNOWN"
    if not (families & pv_families):
        return "UNKNOWN"

    has_liquidity = LIQUIDITY in families
    has_price = PRICE in families

    if operator_set & _VOLATILITY_OPERATORS and not (operator_set & _MOMENTUM_OPERATORS):
        return VOLATILITY
    if has_liquidity and not has_price:
        return LIQUIDITY
    if operator_set & _REVERSAL_OPERATORS:
        return PRICE_REVERSAL
    if operator_set & _MOMENTUM_OPERATORS:
        return PRICE_MOMENTUM
    return "UNKNOWN"


def subexpression_family(
    fields: Iterable[str],
    operators: Iterable[str],
    *,
    resolver: FieldFamilyResolver | None = None,
) -> str:
    """Family of one leg of a combination node.

    Unlike :func:`classify_factor_family` this never returns ``UNKNOWN`` for
    pure price/volume legs — combination keys need a concrete label on every
    side — but still returns ``UNKNOWN`` when a leg carries no classifiable
    field at all (e.g. a bare constant).
    """
    resolver = resolver or FieldFamilyResolver()
    field_list = list(fields)
    if not field_list:
        return "UNKNOWN"
    family = classify_factor_family(field_list, operators, resolver=resolver)
    if family != "UNKNOWN":
        return family
    families = {resolver(field) for field in field_list}
    if PRICE in families:
        return PRICE_MOMENTUM
    if LIQUIDITY in families:
        return LIQUIDITY
    if families <= {OTHER}:
        return OTHER
    return "UNKNOWN"
