"""Deterministic similarity primitives shared by duplicate detection and novelty.

Window comparisons deliberately avoid ``w1 != w2``: 20 vs 21 is the same
horizon while 20 vs 120 is not. Two calibrated views are provided:

* horizon buckets (1-5 / 6-20 / 21-60 / 61-120 / 121-252 / 252+), and
* a continuous ``abs(log(w1/w2))`` distance.

Two layers deliberately lost by a plain Jaccard are recovered here:

* **Field families** — ``close`` and ``vwap`` share zero characters in an
  exact-name set, but both are ``PRICE`` fields, so
  ``rank(ts_delta(close,10))`` stays similar to
  ``rank(ts_delta(vwap,10))``.
* **Operator paths** — an operator *set* cannot tell
  ``rank(ts_mean(ts_delta(x,5),20))`` from
  ``ts_mean(rank(ts_delta(x,5)),20)``; root-to-leaf operator chains can.

All weights come from :class:`~worldquant.registry.config.SimilarityConfig`.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Iterable, Sequence

from .config import SimilarityConfig

#: Configurable horizon bucket edges (inclusive upper bounds).
HORIZON_BUCKETS: tuple[int, ...] = (5, 20, 60, 120, 252)


def horizon_bucket(window: int) -> int:
    for index, edge in enumerate(HORIZON_BUCKETS):
        if window <= edge:
            return index
    return len(HORIZON_BUCKETS)


def window_log_distance(w1: float, w2: float) -> float:
    """Scale-free window distance; 20/21 -> 0.05, 20/120 -> 1.79."""
    if w1 <= 0 or w2 <= 0:
        return float("inf")
    return abs(math.log(w1 / w2))


def jaccard(left: Iterable, right: Iterable) -> float:
    a = set(left)
    b = set(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def multiset_jaccard(left: Counter | dict, right: Counter | dict) -> float:
    """Jaccard over multiplicities: min-count intersection / max-count union."""
    a = Counter(dict(left))
    b = Counter(dict(right))
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    intersection = sum((a & b).values())
    union = sum((a | b).values())
    return intersection / union if union else 0.0


def window_set_similarity(windows_a: Sequence[int], windows_b: Sequence[int]) -> float:
    """Similarity of two window sets in ``[0, 1]``.

    A horizon-bucket Jaccard gives the categorical view, and a matched
    log-distance term keeps 20-vs-21 closer than 20-vs-60 even inside one edge.
    """
    if not windows_a and not windows_b:
        return 1.0
    if not windows_a or not windows_b:
        return 0.0

    bucket_sim = jaccard(
        (horizon_bucket(w) for w in windows_a),
        (horizon_bucket(w) for w in windows_b),
    )

    # For each candidate window take the closest historical window; average the
    # exp(-distance) matches, symmetrized over both sets.
    def one_sided(source: Sequence[int], target: Sequence[int]) -> float:
        return sum(
            math.exp(-min(window_log_distance(w, other) for other in target))
            for w in source
        ) / len(source)

    distance_sim = 0.5 * (
        one_sided(windows_a, windows_b) + one_sided(windows_b, windows_a)
    )
    return 0.6 * bucket_sim + 0.4 * distance_sim


def depth_similarity(depth_a: int, depth_b: int) -> float:
    if depth_a == 0 and depth_b == 0:
        return 1.0
    return 1.0 - abs(depth_a - depth_b) / max(depth_a, depth_b, 1)


def field_similarity(
    fields_a: Iterable[str],
    fields_b: Iterable[str],
    families_a: Iterable[str] | None = None,
    families_b: Iterable[str] | None = None,
    config: SimilarityConfig | None = None,
) -> float:
    """Two-layer field similarity.

    ``exact`` compares concrete field names; ``family`` compares the
    field-family *multiset* (so close/open/vwap all collapse into PRICE).
    """
    cfg = config or SimilarityConfig()
    weights = cfg.normalized()
    exact_sim = jaccard(fields_a, fields_b)
    if families_a is not None and families_b is not None:
        family_sim = multiset_jaccard(Counter(families_a), Counter(families_b))
        we, wf = weights["field_exact"], weights["field_family"]
        total = we + wf
        if total <= 0:
            return exact_sim
        return (we * exact_sim + wf * family_sim) / total
    return exact_sim


def _weighted(parts: list[tuple[float, float]]) -> float:
    total = sum(weight for _, weight in parts)
    if total <= 0:
        return 0.0
    return sum(value * weight for value, weight in parts) / total


def structure_feature_similarity(
    operators_a: set[str],
    operators_b: set[str],
    root_a: str,
    root_b: str,
    depth_a: int,
    depth_b: int,
    *,
    multiset_a: Counter | dict | None = None,
    multiset_b: Counter | dict | None = None,
    paths_a: Sequence[str] | None = None,
    paths_b: Sequence[str] | None = None,
    config: SimilarityConfig | None = None,
) -> float:
    """Feature-level structure similarity when structure hashes differ.

    Operator *set* Jaccard is deliberately just one term: multiset counts
    repeated transforms and root-to-leaf paths preserve AST nesting order.
    """
    cfg = config or SimilarityConfig()
    w = cfg.normalized()
    parts: list[tuple[float, float]] = [
        (jaccard(operators_a, operators_b), w["operator_set"]),
        (1.0 if root_a and root_a == root_b else 0.0, w["root"]),
        (depth_similarity(depth_a, depth_b), w["depth"]),
    ]
    if multiset_a is not None and multiset_b is not None:
        parts.append(
            (multiset_jaccard(multiset_a, multiset_b), w["operator_multiset"])
        )
    if paths_a is not None and paths_b is not None:
        parts.append((jaccard(paths_a, paths_b), w["path"]))
    return _weighted(parts)


def overall_similarity(
    *,
    structure_equal: bool,
    operators_a: set[str],
    operators_b: set[str],
    root_a: str,
    root_b: str,
    depth_a: int,
    depth_b: int,
    fields_a: set[str],
    fields_b: set[str],
    windows_a: Sequence[int],
    windows_b: Sequence[int],
    families_a: Iterable[str] | None = None,
    families_b: Iterable[str] | None = None,
    multiset_a: Counter | dict | None = None,
    multiset_b: Counter | dict | None = None,
    paths_a: Sequence[str] | None = None,
    paths_b: Sequence[str] | None = None,
    config: SimilarityConfig | None = None,
) -> float:
    """The 0..1 score used by ``find_similar`` / structure duplicate level."""
    cfg = config or SimilarityConfig()
    weights = cfg.normalized()
    if structure_equal:
        structure_sim = 1.0
    else:
        structure_sim = structure_feature_similarity(
            operators_a, operators_b, root_a, root_b, depth_a, depth_b,
            multiset_a=multiset_a, multiset_b=multiset_b,
            paths_a=paths_a, paths_b=paths_b, config=cfg,
        )
    field_sim = field_similarity(
        fields_a, fields_b, families_a, families_b, config=cfg
    )
    window_sim = window_set_similarity(list(windows_a), list(windows_b))
    return _weighted(
        [
            (structure_sim, weights["overall_structure"]),
            (field_sim, weights["overall_field"]),
            (window_sim, weights["overall_window"]),
        ]
    )
