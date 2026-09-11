"""Deterministic similarity primitives shared by duplicate detection and novelty.

Window comparisons deliberately avoid ``w1 != w2``: 20 vs 21 is the same
horizon while 20 vs 120 is not. Two calibrated views are provided:

* horizon buckets (1-5 / 6-20 / 21-60 / 61-120 / 121-252 / 252+), and
* a continuous ``abs(log(w1/w2))`` distance.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

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


def structure_feature_similarity(
    operators_a: set[str],
    operators_b: set[str],
    root_a: str,
    root_b: str,
    depth_a: int,
    depth_b: int,
) -> float:
    """Feature-level structure similarity when structure hashes differ."""
    operator_sim = jaccard(operators_a, operators_b)
    root_sim = 1.0 if root_a and root_a == root_b else 0.0
    depth_sim = depth_similarity(depth_a, depth_b)
    return 0.6 * operator_sim + 0.25 * root_sim + 0.15 * depth_sim


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
) -> float:
    """The 0..1 score used by ``find_similar`` / structure duplicate level."""
    if structure_equal:
        structure_sim = 1.0
    else:
        structure_sim = structure_feature_similarity(
            operators_a, operators_b, root_a, root_b, depth_a, depth_b
        )
    field_sim = jaccard(fields_a, fields_b)
    window_sim = window_set_similarity(list(windows_a), list(windows_b))
    return 0.5 * structure_sim + 0.3 * field_sim + 0.2 * window_sim
