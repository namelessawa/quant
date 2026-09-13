"""Correlation clusters via connected components.

No machine learning in v1: an edge exists between two factors when their stored
correlation magnitude is at least ``ClusteringConfig.corr_threshold`` (default
0.70), and a simple union-find produces the connected components. Each cluster
gets exactly one representative so a generation agent never receives a pile of
near-identical high-Sharpe alphas.

The cutoff builds the GRAPH only. Averaging graph edges would overstate cluster
tightness (every edge is above the cutoff by construction), so each cluster
also carries unbiased statistics over every OBSERVED member pair:
``mean_abs_corr``, ``known_pair_coverage`` and ``high_corr_density``. An
unobserved pair is an evidence gap that lowers coverage/density — it is never
counted as zero correlation. Saturation is decided from cluster size +
coverage + (mean |corr| OR high-corr density), with all thresholds in
``ClusteringConfig``.

Representative priority:

1. a SUBMITTED alpha (the protected anchor of the cluster), then
2. Fitness DESC, Sharpe DESC, Turnover ASC, Drawdown ASC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import CORR_TYPE_SELF, FactorRegistry, FactorStatus


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, node: int) -> None:
        if node not in self.parent:
            self.parent[node] = node

    def find(self, node: int) -> int:
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, a: int, b: int) -> None:
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class CorrelationCluster:
    cluster_id: int
    factor_ids: list[int]
    representative_id: int
    size: int
    submitted_count: int
    #: Enrichment (populated by :func:`get_clusters`).
    family_distribution: dict[str, int] | None = None
    field_distribution: dict[str, int] | None = None
    template_distribution: dict[str, int] | None = None
    best_sharpe: float | None = None
    best_fitness: float | None = None
    #: Mean |corr| over the GRAPH edges only (every one is >= cutoff by
    #: construction). Kept for backward compatibility/reporting; it is
    #: upward-biased. Use ``mean_abs_corr`` for the unbiased cluster statistic.
    avg_corr: float | None = None
    max_corr: float | None = None
    # ------------------------------------------------------------------ #
    # Unbiased whole-cluster statistics. The cutoff is used ONLY to build
    # the connected-component graph; these numbers consider every observed
    # pairwise correlation between members, and unobserved pairs are NEVER
    # counted as zero correlation — they reduce coverage instead.
    # ------------------------------------------------------------------ #
    #: Number of member pairs with a stored observed |corr| (any magnitude).
    known_pair_count: int = 0
    #: n*(n-1)/2 for the cluster.
    total_pair_count: int = 0
    #: Mean |corr| over all OBSERVED member pairs (None when none observed).
    mean_abs_corr: float | None = None
    #: known_pair_count / total_pair_count — how much of the pair space we
    #: actually have evidence for.
    known_pair_coverage: float | None = None
    #: Fraction of ALL member pairs observed at/above the graph cutoff (not a
    #: fraction of observed pairs). A long 0.71-chain with weak end-to-end
    #: links therefore has low density even though every graph edge is high.
    high_corr_density: float | None = None
    saturated: bool = False
    theme: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "representative_id": self.representative_id,
            "factor_ids": self.factor_ids,
            "size": self.size,
            "submitted_count": self.submitted_count,
            "family_distribution": self.family_distribution,
            "field_distribution": self.field_distribution,
            "template_distribution": self.template_distribution,
            "best_sharpe": self.best_sharpe,
            "best_fitness": self.best_fitness,
            "avg_corr": self.avg_corr,
            "max_corr": self.max_corr,
            "known_pair_count": self.known_pair_count,
            "total_pair_count": self.total_pair_count,
            "mean_abs_corr": self.mean_abs_corr,
            "known_pair_coverage": self.known_pair_coverage,
            "high_corr_density": self.high_corr_density,
            "saturated": self.saturated,
            "theme": self.theme,
        }


def _pair_weights(registry: FactorRegistry) -> dict[tuple[int, int], float]:
    """Every observed undirected member-pair -> max |corr|, NO cutoff applied.

    Only *real pairwise* rows enter the map: per-neighbor rows of
    ``correlation_type='SELF'`` whose neighbor resolves to a registry factor
    (directly via ``other_factor_id`` or via a BRAIN alpha id, which also
    covers the imported submitted set). The ``SELF_MAX`` aggregate row has no
    neighbor at all — it exists solely to feed the correlation gate — so it can
    never create an edge or merge clusters.

    When both directions of a pair were ever recorded, the larger magnitude
    wins (same de-dup rule the graph builder used historically).
    """
    rows = registry._query(
        """
        SELECT factor_id, other_factor_id, other_brain_alpha_id,
               abs_correlation, correlation
        FROM factor_correlations
        WHERE correlation_type = ?
        """,
        (CORR_TYPE_SELF,),
    )
    brain_to_id = {
        str(row["brain_alpha_id"]): int(row["id"])
        for row in registry._query(
            "SELECT id, brain_alpha_id FROM factors "
            "WHERE brain_alpha_id IS NOT NULL"
        )
    }
    pairs: dict[tuple[int, int], float] = {}
    for row in rows:
        factor_id = int(row["factor_id"])
        other_id = row["other_factor_id"]
        if other_id is None and row["other_brain_alpha_id"]:
            other_id = brain_to_id.get(str(row["other_brain_alpha_id"]))
        if other_id is None:
            continue
        other_id = int(other_id)
        if other_id == factor_id:
            continue
        magnitude = row["abs_correlation"]
        if magnitude is None and row["correlation"] is not None:
            magnitude = abs(float(row["correlation"]))
        if magnitude is None:
            continue
        edge = (min(factor_id, other_id), max(factor_id, other_id))
        pairs[edge] = max(pairs.get(edge, 0.0), float(magnitude))
    return pairs


def _edges(
    registry: FactorRegistry,
    threshold: float,
    pair_weights: dict[tuple[int, int], float] | None = None,
) -> list[tuple[int, int, float]]:
    """Undirected edges at/above the cutoff (graph construction only)."""
    weights = pair_weights if pair_weights is not None else _pair_weights(registry)
    return [
        (a, b, weight)
        for (a, b), weight in weights.items()
        if weight >= threshold
    ]


def get_clusters(
    registry: FactorRegistry,
    *,
    threshold: float | None = None,
    min_size: int = 1,
) -> list[CorrelationCluster]:
    """Return correlation clusters, largest first."""
    cutoff = (
        threshold
        if threshold is not None
        else registry.config.clustering.corr_threshold
    )
    uf = _UnionFind()

    # Every factor starts as a singleton cluster; edges then merge them. This
    # matters for representative selection: an isolated SUBMITTED alpha is its
    # own cluster of size 1, and ``min_size`` below controls whether callers see
    # those singletons.
    for row in registry._query("SELECT id FROM factors"):
        uf.add(int(row["id"]))

    # Fetch all observed pairwise weights ONCE: the graph cutoff is applied to
    # a copy for connected components, while the unbiased statistics use the
    # unfiltered map (unobserved pairs are never treated as zero corr).
    pair_weights = _pair_weights(registry)
    edges = _edges(registry, float(cutoff), pair_weights=pair_weights)
    edge_lookup = {
        (min(a, b), max(a, b)): weight for a, b, weight in edges
    }
    for a, b, _ in edges:
        uf.add(a)
        uf.add(b)
        uf.union(a, b)

    groups: dict[int, list[int]] = {}
    for node in uf.parent:
        groups.setdefault(uf.find(node), []).append(node)

    metrics = {
        int(row["factor_id"]): row
        for row in registry._query(
            """
            SELECT m.factor_id AS factor_id, m.sharpe, m.fitness, m.turnover,
                   m.drawdown, f.status, f.brain_alpha_id
            FROM factor_metrics m JOIN factors f ON f.id = m.factor_id
            """
        )
    }
    factors = {
        int(row["id"]): row
        for row in registry._query("SELECT id, status FROM factors")
    }

    def representative_key(factor_id: int) -> tuple:
        row = factors.get(factor_id)
        metric = metrics.get(factor_id)
        submitted_priority = (
            0 if row is not None and row["status"] == FactorStatus.SUBMITTED else 1
        )

        def value(key: str) -> float:
            raw = metric[key] if metric is not None else None
            return float(raw) if raw is not None else float("-inf")

        def ascending(key: str) -> float:
            raw = metric[key] if metric is not None else None
            return abs(float(raw)) if raw is not None else float("inf")

        # Smaller tuple wins: submitted first, fitness/sharpe descending,
        # turnover/drawdown ascending, factor id as a deterministic tiebreak.
        return (
            submitted_priority,
            -value("fitness"),
            -value("sharpe"),
            ascending("turnover"),
            ascending("drawdown"),
            factor_id,
        )

    clusters: list[CorrelationCluster] = []
    for members in groups.values():
        if len(members) < min_size:
            continue
        ordered = sorted(members)
        representative = min(ordered, key=representative_key)
        submitted_count = sum(
            1
            for fid in ordered
            if (row := factors.get(fid)) is not None
            and row["status"] == FactorStatus.SUBMITTED
        )

        member_metrics = [
            metrics[fid] for fid in ordered if fid in metrics
        ]
        sharpes = [
            float(row["sharpe"]) for row in member_metrics
            if row["sharpe"] is not None
        ]
        fitnesses = [
            float(row["fitness"]) for row in member_metrics
            if row["fitness"] is not None
        ]
        best_sharpe = max(sharpes) if sharpes else None
        best_fitness = max(fitnesses) if fitnesses else None

        internal_weights = [
            weight
            for i, a in enumerate(ordered)
            for b in ordered[i + 1:]
            if (weight := edge_lookup.get((a, b))) is not None
        ]
        avg_corr = (
            sum(internal_weights) / len(internal_weights)
            if internal_weights else None
        )

        # Unbiased statistics over EVERY observed member pair (graph cutoff
        # plays no role here). Pairs without a stored row are simply absent —
        # they lower coverage/density instead of being counted as corr == 0.
        known_weights = [
            weight
            for i, a in enumerate(ordered)
            for b in ordered[i + 1:]
            if (weight := pair_weights.get((a, b))) is not None
        ]
        total_pairs = len(ordered) * (len(ordered) - 1) // 2
        known_pairs = len(known_weights)
        mean_abs_corr = (
            sum(known_weights) / known_pairs if known_pairs else None
        )
        coverage = known_pairs / total_pairs if total_pairs else 1.0
        high_pairs = sum(1 for weight in known_weights if weight >= float(cutoff))
        high_corr_density = high_pairs / total_pairs if total_pairs else 0.0
        max_corr = max(known_weights) if known_weights else None

        family_dist, field_dist, template_dist, theme = _distributions(
            registry, ordered
        )

        # Saturation rule (all thresholds configurable via ClusteringConfig):
        # enough members AND enough observed pair coverage AND the observed
        # pairs are tight on average OR the dense-high-corr share is large.
        # A 0.71 chain with weak end-to-end links merges into one graph cluster
        # but is NOT saturated: coverage may be full, but mean_abs_corr and
        # high_corr_density both expose the weak link.
        cluster_cfg = registry.config.clustering
        saturated = (
            len(ordered) >= cluster_cfg.saturated_min_size
            and coverage >= cluster_cfg.saturated_min_coverage
            and (
                (
                    mean_abs_corr is not None
                    and mean_abs_corr >= cluster_cfg.saturated_mean_abs_corr
                )
                or high_corr_density >= cluster_cfg.saturated_high_corr_density
            )
        )

        clusters.append(
            CorrelationCluster(
                cluster_id=representative,
                factor_ids=ordered,
                representative_id=representative,
                size=len(ordered),
                submitted_count=submitted_count,
                family_distribution=family_dist,
                field_distribution=field_dist,
                template_distribution=template_dist,
                best_sharpe=best_sharpe,
                best_fitness=best_fitness,
                avg_corr=avg_corr,
                max_corr=max_corr,
                known_pair_count=known_pairs,
                total_pair_count=total_pairs,
                mean_abs_corr=mean_abs_corr,
                known_pair_coverage=coverage,
                high_corr_density=high_corr_density,
                saturated=saturated,
                theme=theme,
            )
        )
    clusters.sort(key=lambda cluster: cluster.size, reverse=True)
    return clusters


def _distributions(
    registry: FactorRegistry, member_ids: list[int]
) -> tuple[dict[str, int], dict[str, int], dict[str, int], str | None]:
    """Family / concrete-field / template histograms + dominant theme."""
    import json
    from collections import Counter

    if not member_ids:
        return {}, {}, {}, None
    placeholders = ",".join("?" for _ in member_ids)
    rows = registry._query(
        f"""
        SELECT f.family_template, f.theme, ff.fields_json, ff.factor_family
        FROM factors f LEFT JOIN factor_features ff ON ff.factor_id = f.id
        WHERE f.id IN ({placeholders})
        """,
        tuple(member_ids),
    )
    family_counter: Counter = Counter()
    field_counter: Counter = Counter()
    template_counter: Counter = Counter()
    theme_counter: Counter = Counter()
    for row in rows:
        if row["factor_family"]:
            family_counter[str(row["factor_family"])] += 1
        if row["family_template"]:
            template_counter[str(row["family_template"])] += 1
        if row["theme"]:
            theme_counter[str(row["theme"])] += 1
        try:
            fields = json.loads(row["fields_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            fields = []
        for name in fields:
            field_counter[str(name)] += 1
    theme = theme_counter.most_common(1)[0][0] if theme_counter else None
    return (
        dict(family_counter.most_common(10)),
        dict(field_counter.most_common(15)),
        dict(template_counter.most_common(10)),
        theme,
    )


def get_cluster_representatives(
    registry: FactorRegistry,
    *,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """One best alpha per cluster (used by generation context / report)."""
    representatives: list[dict[str, Any]] = []
    for cluster in get_clusters(registry, threshold=threshold, min_size=1):
        factor = registry.get_factor(cluster.representative_id) or {}
        metrics = registry.get_metrics(cluster.representative_id) or {}
        representatives.append(
            {
                "cluster_id": cluster.cluster_id,
                "representative_id": cluster.representative_id,
                "size": cluster.size,
                "submitted_count": cluster.submitted_count,
                "expression": factor.get("expression"),
                "status": factor.get("status"),
                "brain_alpha_id": factor.get("brain_alpha_id"),
                "sharpe": metrics.get("sharpe"),
                "fitness": metrics.get("fitness"),
                "turnover": metrics.get("turnover"),
                "drawdown": metrics.get("drawdown"),
                "members": cluster.factor_ids,
                "family_distribution": cluster.family_distribution,
                "field_distribution": cluster.field_distribution,
                "template_distribution": cluster.template_distribution,
                "best_sharpe": cluster.best_sharpe,
                "best_fitness": cluster.best_fitness,
                "avg_corr": cluster.avg_corr,
                "max_corr": cluster.max_corr,
                "known_pair_count": cluster.known_pair_count,
                "total_pair_count": cluster.total_pair_count,
                "mean_abs_corr": cluster.mean_abs_corr,
                "known_pair_coverage": cluster.known_pair_coverage,
                "high_corr_density": cluster.high_corr_density,
                "saturated": cluster.saturated,
                "theme": cluster.theme,
            }
        )
    return representatives
