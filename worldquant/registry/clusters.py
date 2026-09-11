"""Correlation clusters via connected components.

No machine learning in v1: an edge exists between two factors when their stored
correlation magnitude is at least ``ClusteringConfig.corr_threshold`` (default
0.70), and a simple union-find produces the connected components. Each cluster
gets exactly one representative so a generation agent never receives a pile of
near-identical high-Sharpe alphas.

Representative priority:

1. a SUBMITTED alpha (the protected anchor of the cluster), then
2. Fitness DESC, Sharpe DESC, Turnover ASC, Drawdown ASC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import CORR_TYPE_SELF, CORR_TYPE_SELF_MAX, FactorRegistry, FactorStatus


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


def _edges(registry: FactorRegistry, threshold: float) -> list[tuple[int, int, float]]:
    """Undirected edges above the cutoff.

    Two edge sources are merged:
    * local links (``other_factor_id`` resolved to a registry factor);
    * remote links (``other_brain_alpha_id`` matching another registered
      factor's BRAIN id), which cover the imported submitted set.
    """
    rows = registry._query(
        """
        SELECT factor_id, other_factor_id, other_brain_alpha_id,
               abs_correlation, correlation
        FROM factor_correlations
        WHERE COALESCE(abs_correlation, ABS(COALESCE(correlation, 0.0))) >= ?
        """,
        (threshold,),
    )
    brain_to_id = {
        str(row["brain_alpha_id"]): int(row["id"])
        for row in registry._query(
            "SELECT id, brain_alpha_id FROM factors "
            "WHERE brain_alpha_id IS NOT NULL"
        )
    }
    edges: dict[tuple[int, int], float] = {}
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
        if magnitude is None or magnitude < threshold:
            continue
        edge = (min(factor_id, other_id), max(factor_id, other_id))
        edges[edge] = max(edges.get(edge, 0.0), float(magnitude))
    return [(a, b, weight) for (a, b), weight in edges.items()]


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

    edges = _edges(registry, float(cutoff))
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
        clusters.append(
            CorrelationCluster(
                cluster_id=representative,
                factor_ids=ordered,
                representative_id=representative,
                size=len(ordered),
                submitted_count=submitted_count,
            )
        )
    clusters.sort(key=lambda cluster: cluster.size, reverse=True)
    return clusters


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
            }
        )
    return representatives
