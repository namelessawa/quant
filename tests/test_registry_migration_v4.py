"""v3 -> v4 registry migration tests.

v4 adds ``corr_evidence_final_at`` so a factor whose read-only
``GET /alphas/{id}/check`` returns final SELF_CORRELATION evidence — including
the zero-neighbour PASS — leaves the backfill target set. Without the marker,
alphas that genuinely have no pairwise edges would be re-fetched forever.
"""

from __future__ import annotations

import sqlite3

from worldquant.registry import FactorRegistry
from worldquant.registry import migrations
from worldquant.registry.config import RegistryConfig
from worldquant.registry.store import CORR_TYPE_SELF

from test_registry_migration_v3 import _build_v2_database

SETTINGS = {"region": "USA", "universe": "TOP3000", "delay": 1}


def test_v2_database_is_brought_to_v4_with_final_evidence_column(tmp_path):
    db_path = tmp_path / "factor_registry.db"
    _build_v2_database(db_path)

    with FactorRegistry(db_path, RegistryConfig()):
        pass

    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        columns = migrations.table_columns(raw, "factors")
        assert "corr_evidence_final_at" in columns
        assert "corr_evidence_note" in columns
        indexes = {
            row[1]
            for row in raw.execute("PRAGMA index_list(factors)").fetchall()
        }
        assert "idx_factors_corr_final" in indexes
    finally:
        raw.close()

    # Reopening is a no-op and keeps the marker columns.
    with FactorRegistry(db_path, RegistryConfig()):
        raw = sqlite3.connect(str(db_path))
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        finally:
            raw.close()


class TestBackfillTargetFinality:
    def _register(self, registry, expression, alpha_id):
        candidate = registry.register_candidate(
            expression, SETTINGS, brain_alpha_id=alpha_id
        )
        return candidate.factor_id

    def test_fresh_brain_linked_factors_are_targets(self, tmp_path):
        with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
            self._register(registry, "rank(close)", "A1")
            self._register(registry, "rank(open)", "A2")
            targets = registry.list_corr_backfill_targets(limit=10)
            assert {f["brain_alpha_id"] for f in targets} == {"A1", "A2"}

    def test_final_evidence_marker_removes_zero_neighbour_factor(self, tmp_path):
        with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
            f1 = self._register(registry, "rank(close)", "A1")
            f2 = self._register(registry, "rank(open)", "A2")

            # /check returned a final zero-neighbour PASS for f1.
            registry.mark_corr_evidence_final(f1)

            targets = registry.list_corr_backfill_targets(limit=10)
            assert [f["id"] for f in targets] == [f2]

            # PENDING-style plain attempts (no finality) must not remove it.
            registry.mark_corr_checked(f2)
            targets = registry.list_corr_backfill_targets(limit=10)
            assert [f["id"] for f in targets] == [f2]

    def test_self_edges_remove_a_factor_even_without_marker(self, tmp_path):
        with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
            f1 = self._register(registry, "rank(close)", "A1")
            other = self._register(registry, "rank(open)", "A2")
            registry.save_correlations(
                f1,
                [{"other_factor_id": other, "correlation": 0.85}],
                CORR_TYPE_SELF,
            )
            targets = registry.list_corr_backfill_targets(limit=10)
            assert [f["id"] for f in targets] == [other]

    def test_error_attempt_keeps_factor_as_a_target(self, tmp_path):
        with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
            f1 = self._register(registry, "rank(close)", "A1")
            registry.mark_corr_checked(f1, error="rate limited")
            targets = registry.list_corr_backfill_targets(limit=10)
            assert [f["id"] for f in targets] == [f1]
