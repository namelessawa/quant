"""Compact research memory for an Alpha Generation agent.

``build_generation_context`` never dumps the whole database: it returns a small,
diversity-controlled digest (saturated families, underexplored families,
overused templates, high-correlation combinations, a few recent failures and
exactly one high-quality representative per correlation cluster). High-Sharpe
alphas correlated at 0.9+ collapse to a single example so a generator cannot be
pulled into one local mode.
"""

from __future__ import annotations

from typing import Any

from .clusters import get_cluster_representatives
from .store import FactorRegistry, FactorStatus

#: A family/combination is called "high correlation" past this average.
HIGH_CORR_AVERAGE = 0.70
SATURATED_FAMILY_MIN_TRIALS = 20
SATURATED_FAMILY_MAX_SUBMIT_RATE = 0.10
UNDEREXPLORED_FAMILY_MAX_TRIALS = 5
OVERUSED_STRUCTURE_MIN_TRIALS = 5


def _top_overused_structures(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    rows = registry._query(
        """
        SELECT family_template, COUNT(*) AS trials,
               SUM(CASE WHEN status = 'SUBMITTED' THEN 1 ELSE 0 END) AS submitted
        FROM factors
        WHERE family_template IS NOT NULL
          AND status IN ('SIMULATED','METRIC_REJECTED','CORR_REJECTED',
                         'PASSED','SUBMITTED','SIMULATION_FAILED','SUBMIT_FAILED')
        GROUP BY family_template
        HAVING trials >= ?
        ORDER BY trials DESC
        LIMIT ?
        """,
        (OVERUSED_STRUCTURE_MIN_TRIALS, limit),
    )
    return [dict(row) for row in rows]


def _top_fields(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    rows = registry._query(
        """
        SELECT ff.fields_json, f.status
        FROM factor_features ff JOIN factors f ON f.id = ff.factor_id
        WHERE f.status IN ('SIMULATED','METRIC_REJECTED','CORR_REJECTED',
                           'PASSED','SUBMITTED')
        """
    )
    counts: dict[str, int] = {}
    for row in rows:
        for field in _safe_json(row["fields_json"]):
            counts[str(field)] = counts.get(str(field), 0) + 1
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return [{"field": name, "trials": count} for name, count in ordered[:limit]]


def _safe_json(raw: str | None) -> list[Any]:
    import json

    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _recent_rejected(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    rows = registry._query(
        """
        SELECT id, expression, status, rejection_reason, family_template
        FROM factors
        WHERE status IN ('CORR_REJECTED', 'METRIC_REJECTED', 'DUPLICATE',
                         'SIMULATION_FAILED')
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def _successful_examples(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    """One PASSED/SUBMITTED example per correlation cluster, best first."""
    representatives = get_cluster_representatives(registry)
    examples = [
        item for item in representatives
        if item["status"] in (FactorStatus.PASSED, FactorStatus.SUBMITTED)
        or (item["sharpe"] is not None and item["fitness"] is not None)
    ]
    examples.sort(
        key=lambda item: (
            0 if item["status"] == FactorStatus.SUBMITTED else 1,
            -(item["fitness"] or float("-inf")),
            -(item["sharpe"] or float("-inf")),
        )
    )
    return examples[:limit]


def build_generation_context(
    registry: FactorRegistry,
    *,
    max_items: int = 10,
    max_examples: int = 5,
) -> dict[str, Any]:
    """Return the compact research-memory digest described in the brief.

    Args:
        max_items: cap for list sections (families/structures/patterns).
        max_examples: cap for rejected/successful example sections.
    """
    family_stats = registry.get_family_stats()

    saturated_families = [
        {
            "family": row["family"],
            "trials": row["trials"],
            "submitted": row["submitted"],
            "avg_sharpe": row["avg_sharpe"],
        }
        for row in family_stats
        if (row["trials"] or 0) >= SATURATED_FAMILY_MIN_TRIALS
        and (row["submitted"] or 0)
        / max(int(row["trials"]), 1) <= SATURATED_FAMILY_MAX_SUBMIT_RATE
    ][:max_items]

    underexplored_families = [
        {
            "family": row["family"],
            "trials": row["trials"],
        }
        for row in registry.get_underexplored_families(
            max_trials=UNDEREXPLORED_FAMILY_MAX_TRIALS
        )
    ][:max_items]

    high_corr_patterns = [
        {
            "combination_key": row["combination_key"],
            "trials": row["trial_count"],
            "submitted": row["submission_count"],
            "avg_abs_corr": row["avg_abs_corr"],
            "best_fitness": row["best_fitness"],
        }
        for row in registry.get_combination_stats()
        if (row["trial_count"] or 0) >= SATURATED_FAMILY_MIN_TRIALS // 2
        and row["avg_abs_corr"] is not None
        and row["avg_abs_corr"] >= HIGH_CORR_AVERAGE
    ][:max_items]

    saturated_combo_keys = {
        row["combination_key"]
        for row in registry.get_saturated_combinations()
    }
    saturated_combinations = [
        {
            "combination_key": row["combination_key"],
            "trials": row["trial_count"],
            "submitted": row["submission_count"],
            "avg_sharpe": row["avg_sharpe"],
            "avg_abs_corr": row["avg_abs_corr"],
        }
        for row in registry.get_combination_stats()
        if row["combination_key"] in saturated_combo_keys
    ][:max_items]

    return {
        "saturated_families": saturated_families,
        "underexplored_families": underexplored_families,
        "overused_structures": _top_overused_structures(registry, max_items),
        "most_common_fields": _top_fields(registry, max_items),
        "high_corr_patterns": high_corr_patterns,
        "saturated_combinations": saturated_combinations,
        "underexplored_combinations": [
            {
                "combination_key": row["combination_key"],
                "trials": row["trial_count"],
            }
            for row in registry.get_underexplored_combinations()
        ][:max_items],
        "recent_rejected_examples": _recent_rejected(registry, max_examples),
        "successful_diverse_examples": _successful_examples(registry, max_examples),
        "summary": registry.stats(),
    }
