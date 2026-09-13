"""Compact research memory for an Alpha Generation agent.

``build_generation_context`` never dumps the whole database: it returns a small,
diversity-controlled digest (saturated families, underexplored families,
overused templates, high-correlation combinations, a few recent failures and
exactly one high-quality representative per correlation cluster). High-Sharpe
alphas correlated at 0.9+ collapse to a single example so a generator cannot be
pulled into one local mode.

v2 adds the sections a generator needs to actively *avoid repeating itself*:

* ``do_not_repeat`` — saturated clusters and high-corr combinations with
  pattern / reason / trials / best_corr,
* ``saturated_clusters`` / ``failed_research_directions`` /
  ``high_corr_directions`` — structured dead ends,
* ``underexplored_datasets`` / ``underexplored_operator_structures`` /
  ``underexplored_families`` — open space,
* ``high_quality_redundant_examples`` — strong signals to keep while changing
  the data/legs rather than tuning parameters,
* ``successful_diverse_examples`` — one representative per cluster.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .clusters import get_cluster_representatives, get_clusters
from .failures import FAILURE_LABELS
from .store import FactorRegistry, FactorStatus


def _safe_json(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _top_overused_structures(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    min_trials = registry.config.memory.overused_structure_min_trials
    rows = registry._query(
        """
        SELECT family_template, family_template_hash, COUNT(*) AS trials,
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
        (min_trials, limit),
    )
    return [dict(row) for row in rows]


def _underexplored_structures(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    max_trials = registry.config.memory.underexplored_family_max_trials
    rows = registry._query(
        """
        SELECT family_template, COUNT(*) AS trials
        FROM factors
        WHERE family_template IS NOT NULL
          AND status IN ('SIMULATED','METRIC_REJECTED','CORR_REJECTED',
                         'PASSED','SUBMITTED')
        GROUP BY family_template
        HAVING trials <= ?
        ORDER BY trials ASC
        LIMIT ?
        """,
        (max_trials, limit),
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


def _underexplored_datasets(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    """Datasets present in the field catalog but almost untouched by trials."""
    catalog = dict(getattr(registry.resolver, "field_datasets", None) or {})
    if not catalog:
        return []
    max_trials = registry.config.memory.underexplored_dataset_max_trials
    rows = registry._query(
        """
        SELECT ff.fields_json
        FROM factor_features ff JOIN factors f ON f.id = ff.factor_id
        WHERE f.status IN ('SIMULATED','METRIC_REJECTED','CORR_REJECTED',
                           'PASSED','SUBMITTED')
        """
    )
    counts: Counter = Counter()
    for row in rows:
        for name in _safe_json(row["fields_json"]):
            dataset = catalog.get(str(name)) or catalog.get(str(name).lower())
            if dataset:
                counts[dataset] += 1
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for dataset in sorted(set(catalog.values())):
        if dataset in seen:
            continue
        seen.add(dataset)
        trials = int(counts.get(dataset, 0))
        if trials <= max_trials:
            out.append({"dataset": dataset, "trials": trials})
    out.sort(key=lambda item: (item["trials"], item["dataset"]))
    return out[:limit]


def _recent_rejected(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    rows = registry._query(
        """
        SELECT id, expression, status, rejection_reason, family_template,
               failure_category, corr_band, corr_margin
        FROM factors
        WHERE status IN ('CORR_REJECTED', 'METRIC_REJECTED', 'DUPLICATE',
                         'SIMULATION_FAILED')
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def _failed_directions(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    """Aggregate rejected factors by failure category (+ family breakdown)."""
    rows = registry._query(
        """
        SELECT failure_category, ff.factor_family, COUNT(*) AS n
        FROM factors f
        LEFT JOIN factor_features ff ON ff.factor_id = f.id
        WHERE f.status IN ('METRIC_REJECTED','CORR_REJECTED','SIMULATION_FAILED')
        GROUP BY failure_category, ff.factor_family
        ORDER BY n DESC
        """
    )
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"category": None, "trials": 0, "families": Counter()}
    )
    for row in rows:
        category = row["failure_category"] or "UNKNOWN_FAILURE"
        bucket = grouped[category]
        bucket["category"] = category
        bucket["trials"] += int(row["n"])
        bucket["families"][row["factor_family"] or "UNKNOWN"] += int(row["n"])
    out = []
    for bucket in grouped.values():
        out.append(
            {
                "category": bucket["category"],
                "label": FAILURE_LABELS.get(bucket["category"], bucket["category"]),
                "trials": bucket["trials"],
                "families": dict(bucket["families"].most_common(5)),
            }
        )
    out.sort(key=lambda item: item["trials"], reverse=True)
    return out[:limit]


def _saturated_clusters(registry: FactorRegistry) -> list[dict[str, Any]]:
    return [
        cluster.to_dict()
        for cluster in get_clusters(registry, min_size=2)
        if cluster.saturated
    ]


def _do_not_repeat(
    registry: FactorRegistry,
    saturated_clusters: list[dict[str, Any]],
    high_corr_combos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Explicit avoid-list consumable as a DO_NOT_REPEAT block."""
    avoid: list[dict[str, Any]] = []
    for cluster in saturated_clusters:
        avoid.append(
            {
                "pattern": (
                    f"correlation cluster #{cluster['cluster_id']} "
                    f"({cluster.get('theme') or 'mixed'}): "
                    f"{list((cluster.get('family_distribution') or {}).keys())[:3]}"
                ),
                "reason": "saturated cluster: high internal self-correlation",
                "trials": cluster["size"],
                "best_corr": cluster.get("max_corr"),
                "mean_abs_corr": cluster.get("mean_abs_corr"),
                "known_pair_coverage": cluster.get("known_pair_coverage"),
                "high_corr_density": cluster.get("high_corr_density"),
                "kind": "cluster",
            }
        )
    for combo in high_corr_combos:
        avoid.append(
            {
                "pattern": combo["combination_key"],
                "reason": "leg combination repeatedly collides with existing alphas",
                "trials": combo["trial_count"],
                "best_corr": combo.get("avg_abs_corr"),
                "kind": "combination",
            }
        )
    return avoid


def _successful_examples(registry: FactorRegistry, limit: int) -> list[dict[str, Any]]:
    """One PASSED/SUBMITTED example per correlation cluster, best first.

    Only factors that cleared BOTH the metric and research-correlation gates
    count as success. A SIMULATED factor with corr_status=UNKNOWN is unverified
    evidence, no matter how good its sharpe/fitness look, so it is never offered
    to a generator as a "successful diverse" pattern. High-quality but
    corr-rejected factors surface separately as high_quality_redundant examples.
    """
    representatives = get_cluster_representatives(registry)
    examples = [
        item for item in representatives
        if item["status"] in (FactorStatus.PASSED, FactorStatus.SUBMITTED)
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
    memory = registry.config.memory
    family_stats = registry.get_family_stats()

    saturated_families = [
        {
            "family": row["family"],
            "trials": row["trials"],
            "submitted": row["submitted"],
            "avg_sharpe": row["avg_sharpe"],
        }
        for row in family_stats
        if (row["trials"] or 0) >= memory.saturated_family_min_trials
        and (row["submitted"] or 0)
        / max(int(row["trials"]), 1) <= memory.saturated_family_max_submit_rate
    ][:max_items]

    underexplored_families = [
        {
            "family": row["family"],
            "trials": row["trials"],
        }
        for row in registry.get_underexplored_families(
            max_trials=memory.underexplored_family_max_trials
        )
    ][:max_items]

    combo_stats = registry.get_combination_stats()
    high_corr_patterns = [
        {
            "combination_key": row["combination_key"],
            "trials": row["trial_count"],
            "submitted": row["submission_count"],
            "avg_abs_corr": row["avg_abs_corr"],
            "best_fitness": row["best_fitness"],
        }
        for row in combo_stats
        if (row["trial_count"] or 0) >= memory.saturated_combo_min_trials // 2
        and row["avg_abs_corr"] is not None
        and row["avg_abs_corr"] >= memory.high_corr_average
    ][:max_items]

    saturated_combo_keys = {
        row["combination_key"]
        for row in registry.get_saturated_combinations(
            min_trials=memory.saturated_combo_min_trials,
            max_submit_rate=memory.saturated_combo_max_submit_rate,
        )
    }
    saturated_combinations = [
        {
            "combination_key": row["combination_key"],
            "trials": row["trial_count"],
            "submitted": row["submission_count"],
            "avg_sharpe": row["avg_sharpe"],
            "avg_abs_corr": row["avg_abs_corr"],
        }
        for row in combo_stats
        if row["combination_key"] in saturated_combo_keys
    ][:max_items]

    saturated_clusters = _saturated_clusters(registry)[:max_items]
    high_quality_redundant = registry.get_high_quality_redundant(
        limit=max_examples
    )

    return {
        "saturated_families": saturated_families,
        "underexplored_families": underexplored_families,
        "overused_structures": _top_overused_structures(registry, max_items),
        "underexplored_operator_structures": _underexplored_structures(
            registry, max_items
        ),
        "underexplored_datasets": _underexplored_datasets(registry, max_items),
        "most_common_fields": _top_fields(registry, max_items),
        "high_corr_patterns": high_corr_patterns,
        "high_corr_directions": high_corr_patterns,
        "saturated_combinations": saturated_combinations,
        "saturated_clusters": saturated_clusters,
        "do_not_repeat": _do_not_repeat(
            registry, saturated_clusters, high_corr_patterns
        ),
        "underexplored_combinations": [
            {
                "combination_key": row["combination_key"],
                "trials": row["trial_count"],
            }
            for row in registry.get_underexplored_combinations(
                max_trials=memory.underexplored_combo_max_trials
            )
        ][:max_items],
        "failed_research_directions": _failed_directions(registry, max_items),
        "recent_failures": _recent_rejected(registry, max_examples),
        "recent_rejected_examples": _recent_rejected(registry, max_examples),
        "high_quality_redundant_examples": [
            {
                "factor_id": row["id"],
                "expression": row.get("expression"),
                "sharpe": row.get("sharpe"),
                "fitness": row.get("fitness"),
                "turnover": row.get("turnover"),
                "corr_margin": row.get("corr_margin"),
                "advice": (
                    "keep the idea; switch legs/dataset/family — do NOT retune "
                    "window/decay/truncation"
                ),
            }
            for row in high_quality_redundant
        ],
        "successful_diverse_examples": _successful_examples(registry, max_examples),
        "summary": registry.stats(),
    }
