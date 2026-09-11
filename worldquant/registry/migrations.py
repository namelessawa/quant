"""Incremental schema migrations for the Factor Registry.

The registry keeps its own SQLite file and must never destroy history. Every
schema change therefore:

1. copies ``factor_registry.db`` to a one-time backup first,
2. applies additive ``ALTER TABLE`` / ``CREATE INDEX`` statements only,
3. backfills new columns without deleting or rewriting existing rows,
4. is idempotent — opening the same database twice is a no-op.

The version is tracked with SQLite's native ``PRAGMA user_version`` (plus a
``schema_meta`` table for human-readable bookkeeping). Python-level backfills
that need the expression parser/family resolver live in
:meth:`FactorRegistry._backfill_v2`; this module owns only backup, DDL and the
pure-SQL part of the backfill.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

#: New factors columns (name -> SQL declaration), applied additively.
FACTOR_COLUMNS: dict[str, str] = {
    "signal_hash": "TEXT",
    "experiment_hash": "TEXT",
    "family_template_hash": "TEXT",
    "ablation_group_id": "TEXT",
    "changed_parameters_json": "TEXT",
    "parent_experiment_id": "INTEGER",
    "high_quality_redundant": "INTEGER NOT NULL DEFAULT 0",
    "failure_category": "TEXT",
    "corr_status": "TEXT",
    "corr_band": "TEXT",
    "corr_margin": "REAL",
    "nearest_cluster_id": "INTEGER",
    "theme": "TEXT",
    "branch": "TEXT",
    "subtheme": "TEXT",
}

FEATURE_COLUMNS: dict[str, str] = {
    "operator_paths_json": "TEXT",
    "operator_multiset_json": "TEXT",
    "field_families_json": "TEXT",
}

COMBINATION_COLUMNS: dict[str, str] = {
    "left_template_hash": "TEXT",
    "right_template_hash": "TEXT",
    "left_structure_hash": "TEXT",
    "right_structure_hash": "TEXT",
    "structure_hash": "TEXT",
}

_EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS factor_subtrees (
    factor_id           INTEGER NOT NULL REFERENCES factors(id) ON DELETE CASCADE,
    subtree_index       INTEGER NOT NULL,
    node_label          TEXT NOT NULL,
    leg_index           INTEGER NOT NULL,
    leg_family          TEXT,
    leg_template_hash   TEXT,
    leg_structure_hash  TEXT,
    leg_template        TEXT,
    leg_structure       TEXT,
    PRIMARY KEY (factor_id, subtree_index, leg_index)
);

CREATE INDEX IF NOT EXISTS idx_factors_signal_hash       ON factors(signal_hash);
CREATE INDEX IF NOT EXISTS idx_factors_experiment_hash   ON factors(experiment_hash);
CREATE INDEX IF NOT EXISTS idx_factors_family_tpl_hash   ON factors(family_template_hash);
CREATE INDEX IF NOT EXISTS idx_factors_failure_category  ON factors(failure_category);
CREATE INDEX IF NOT EXISTS idx_factors_corr_status       ON factors(corr_status);
CREATE INDEX IF NOT EXISTS idx_factors_theme             ON factors(theme);
CREATE INDEX IF NOT EXISTS idx_subtrees_leg_template     ON factor_subtrees(leg_template_hash);
CREATE INDEX IF NOT EXISTS idx_subtrees_leg_structure    ON factor_subtrees(leg_structure_hash);
CREATE INDEX IF NOT EXISTS idx_subtrees_family           ON factor_subtrees(leg_family);

CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT
);
"""


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> list[str]:
    existing = table_columns(conn, table)
    added: list[str] = []
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
            )
            added.append(name)
    return added


def _backup_database(db_path: Path, log: Any) -> Path | None:
    """One-time safety copy. An existing backup is never overwritten."""
    if not db_path.exists():
        return None
    backup = db_path.with_name(db_path.name + f".v{SCHEMA_VERSION - 1}.bak")
    if backup.exists():
        log.info("registry migration backup already exists: %s", backup)
        return backup
    shutil.copy2(db_path, backup)
    log.warning("factor registry backed up before v%d migration: %s",
                SCHEMA_VERSION, backup)
    return backup


def ensure_migrated(
    conn: sqlite3.Connection,
    db_path: str | Path,
    log: Any,
) -> bool:
    """Bring the database to :data:`SCHEMA_VERSION`.

    Returns ``True`` only when this call actually upgraded a pre-v2 database.
    Safe to call on a freshly-created v2 database.
    """
    path = Path(db_path)
    version = user_version(conn)
    if version >= SCHEMA_VERSION:
        # Still make sure additive objects exist (they are all IF NOT EXISTS).
        conn.executescript(_EXTRA_DDL)
        conn.commit()
        return False

    upgraded = False
    if version < 2:
        # Only an existing, populated pre-v2 file needs the safety copy.
        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "factors" in existing_tables:
            _backup_database(path, log)
        added_factors = _add_columns(conn, "factors", FACTOR_COLUMNS)
        added_features = _add_columns(conn, "factor_features", FEATURE_COLUMNS)
        added_combos = _add_columns(
            conn, "factor_combinations", COMBINATION_COLUMNS
        )
        conn.executescript(_EXTRA_DDL)

        # Pure-SQL backfills. experiment_hash is defined identically to the
        # historical exact_hash (SHA256 over canonical expr + settings); the
        # parser-dependent signal/family/template hashes are filled in by
        # FactorRegistry._backfill_v2().
        if added_factors and "experiment_hash" in added_factors:
            conn.execute(
                "UPDATE factors SET experiment_hash = exact_hash "
                "WHERE experiment_hash IS NULL"
            )

        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        upgraded = True
        log.info(
            "registry migrated to schema v2 (factor +%d, feature +%d, combo +%d)",
            len(added_factors), len(added_features), len(added_combos),
        )
    return upgraded
