"""v2 -> v3 registry migration tests.

v2 stored an *expression-only* signal_hash. v3 introduces the explicit
``expression_hash`` layer and redefines ``signal_hash`` to include the
information set (region/universe/delay). These tests build a faithful v2
database (via the v1 fixture plus the real v2 ALTER constants) and prove:

* a one-time ``.v2.bak`` backup is taken and never overwritten,
* ``expression_hash`` is backfilled,
* the old signal_hash is OVERWRITTEN by the scoped identity,
* ``experiment_hash`` is preserved untouched,
* UNKNOWN bookkeeping columns appear with safe defaults,
* reopening is idempotent.
"""

from __future__ import annotations

import json
import sqlite3

from worldquant.hashing import expression_identity, signal_identity
from worldquant.registry import FactorRegistry
from worldquant.registry import migrations
from worldquant.registry.config import RegistryConfig

from test_registry_migration_v2 import _build_v1_database


def _build_v2_database(path) -> None:
    """Promote the hand-built v1 fixture to a genuine v2 database."""
    _build_v1_database(path)
    conn = sqlite3.connect(str(path))
    try:
        migrations._add_columns(conn, "factors", migrations.FACTOR_COLUMNS_V2)
        migrations._add_columns(conn, "factor_features", migrations.FEATURE_COLUMNS)
        migrations._add_columns(
            conn, "factor_combinations", migrations.COMBINATION_COLUMNS
        )
        conn.executescript(migrations._EXTRA_DDL_V2)
        conn.execute(
            "UPDATE factors SET experiment_hash = exact_hash "
            "WHERE experiment_hash IS NULL"
        )
        # The v2 (now-superseded) definition: signal identity was expression-only.
        rows = conn.execute(
            "SELECT id, canonical_expression FROM factors"
        ).fetchall()
        for factor_id, canonical in rows:
            conn.execute(
                "UPDATE factors SET signal_hash = ? WHERE id = ?",
                (expression_identity(canonical), factor_id),
            )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def test_v2_to_v3_backs_up_and_redefines_identity(tmp_path):
    db_path = tmp_path / "factor_registry.db"
    _build_v2_database(db_path)
    backup = db_path.with_name("factor_registry.db.v2.bak")
    assert not backup.exists()

    with FactorRegistry(db_path, RegistryConfig()) as registry:
        rows = registry._query(
            "SELECT id, canonical_expression, settings_json, exact_hash, "
            "expression_hash, signal_hash, experiment_hash, "
            "corr_last_checked_at, corr_check_attempts, corr_error "
            "FROM factors ORDER BY id"
        )
        assert len(rows) == 3
        for row in rows:
            settings = json.loads(row["settings_json"] or "{}")
            # New layer backfilled.
            assert row["expression_hash"] == expression_identity(
                row["canonical_expression"]
            )
            # Old expression-only signal_hash was overwritten; the scoped hash
            # differs because the settings carry region/universe/delay.
            assert row["signal_hash"] == signal_identity(
                row["canonical_expression"], settings
            )
            assert row["signal_hash"] != row["expression_hash"]
            # experiment layer is preserved, not recomputed.
            assert row["experiment_hash"] == row["exact_hash"]
            # UNKNOWN bookkeeping defaults.
            assert row["corr_check_attempts"] == 0
            assert row["corr_last_checked_at"] is None
            assert row["corr_error"] is None

    assert backup.exists()


def test_v2_to_v3_is_idempotent_and_never_overwrites_backup(tmp_path):
    db_path = tmp_path / "factor_registry.db"
    _build_v2_database(db_path)
    backup = db_path.with_name("factor_registry.db.v2.bak")

    with FactorRegistry(db_path, RegistryConfig()):
        pass
    first_size = backup.stat().st_size

    with FactorRegistry(db_path, RegistryConfig()) as registry:
        meta = registry._query(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        )
        assert meta and meta[0]["value"] == str(migrations.SCHEMA_VERSION)
        before = {
            row["id"]: (row["expression_hash"], row["signal_hash"])
            for row in registry._query(
                "SELECT id, expression_hash, signal_hash FROM factors"
            )
        }

    # Second migration/backup must be a no-op.
    with FactorRegistry(db_path, RegistryConfig()) as registry:
        after = {
            row["id"]: (row["expression_hash"], row["signal_hash"])
            for row in registry._query(
                "SELECT id, expression_hash, signal_hash FROM factors"
            )
        }
        assert before == after

    assert backup.stat().st_size == first_size
    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    finally:
        raw.close()
