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
:meth:`FactorRegistry._backfill_v2` / :meth:`FactorRegistry._backfill_v3`; this
module owns only backup, DDL and the pure-SQL part of the backfill.

Migration history:

* v1 -> v2: signal/experiment/family-template hashes, failure/corr/theme
  columns, operator paths/multisets, factor_subtrees.
* v2 -> v3: three-layer identity (``expression_hash`` added, ``signal_hash``
  recomputed to include region/universe/delay), plus correlation-refresh
  bookkeeping columns (``corr_last_checked_at`` / ``corr_check_attempts`` /
  ``corr_error``).
* v3 -> v4: ``corr_evidence_final_at`` marks a factor whose read-only
  ``GET /alphas/{id}/check`` returned *final* SELF_CORRELATION evidence,
  including the zero-neighbour PASS case. Without it, an alpha that genuinely
  has no pairwise edges stays a backfill target forever.
* v4 -> v5: ``corr_evidence_note`` records *why* evidence is final. The
  platform only schedules the SELF_CORRELATION computation once every other
  submission check passes, so an alpha failing e.g. LOW_FITNESS keeps
  SELF_CORRELATION at PENDING forever; an ALREADY_SUBMITTED duplicate never
  exposes a recordset either. Those are terminal "evidence not available on
  the platform" states (corr verdict stays UNKNOWN — never a fabricated
  PASS/FAIL), distinguishable in reports from genuinely-still-pending alphas.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

#: New factors columns added by the v1 -> v2 migration.
FACTOR_COLUMNS_V2: dict[str, str] = {
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

#: New factors columns added by the v2 -> v3 migration.
FACTOR_COLUMNS_V3: dict[str, str] = {
    # Mathematical-expression identity (settings-independent). The v2
    # signal_hash was expression-only; v3 keeps this layer explicitly and
    # redefines signal_hash to include region/universe/delay.
    "expression_hash": "TEXT",
    # Correlation-refresh bookkeeping so UNKNOWN never becomes a permanent,
    # undiagnosable black hole.
    "corr_last_checked_at": "TEXT",
    "corr_check_attempts": "INTEGER NOT NULL DEFAULT 0",
    "corr_error": "TEXT",
}

#: New factors columns added by the v3 -> v4 migration.
FACTOR_COLUMNS_V4: dict[str, str] = {
    # Set when GET /alphas/{id}/check returned final SELF_CORRELATION evidence
    # (PASS/FAIL or a concrete neighbour recordset). A zero-neighbour PASS is
    # complete evidence: the graph is correctly edgeless for that alpha, so it
    # must not be retried on every backfill run.
    "corr_evidence_final_at": "TEXT",
}

#: New factors columns added by the v4 -> v5 migration.
FACTOR_COLUMNS_V5: dict[str, str] = {
    # Why corr evidence was marked final. Real-evidence notes:
    #   "SELF_PASS" / "SELF_FAIL" / "SELF_NEIGHBORS" (concrete recordset).
    # Platform-unavailable notes (corr verdict stays UNKNOWN):
    #   "GATED:<checks>" — SELF_CORRELATION is never scheduled while another
    #      submission check is FAIL;
    #   "ALREADY_SUBMITTED" — a duplicate alpha whose /check exposes no corr.
    "corr_evidence_note": "TEXT",
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

_EXTRA_DDL_V2 = """
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

#: Objects introduced by v2 -> v3 (depends on the expression_hash column).
_EXTRA_DDL_V3 = """
CREATE INDEX IF NOT EXISTS idx_factors_expression_hash   ON factors(expression_hash);
"""

#: Objects introduced by v3 -> v4.
_EXTRA_DDL_V4 = """
CREATE INDEX IF NOT EXISTS idx_factors_corr_final        ON factors(corr_evidence_final_at);
"""

_EXTRA_DDL = _EXTRA_DDL_V2 + _EXTRA_DDL_V3 + _EXTRA_DDL_V4


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


def _has_factors_table(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='factors'"
        ).fetchone()
    )


def _backup_database(db_path: Path, log: Any, from_version: int) -> Path | None:
    """One-time safety copy, named after the version being upgraded FROM.

    An existing backup is never overwritten, so interrupted/repeated upgrades
    always preserve the pristine pre-migration file.
    """
    if not db_path.exists():
        return None
    backup = db_path.with_name(db_path.name + f".v{from_version}.bak")
    if backup.exists():
        log.info("registry migration backup already exists: %s", backup)
        return backup
    shutil.copy2(db_path, backup)
    log.warning(
        "factor registry backed up before v%d migration: %s",
        from_version + 1, backup,
    )
    return backup


def ensure_migrated(
    conn: sqlite3.Connection,
    db_path: str | Path,
    log: Any,
) -> dict[str, Any]:
    """Bring the database up to :data:`SCHEMA_VERSION`.

    Returns ``{"from_version", "to_version", "levels"}`` where ``levels`` lists
    the migration levels applied during THIS call (subset of ``{2, 3, 4}``); an
    empty list means the database was already current. Safe to call on a
    freshly-created latest-schema database.
    """
    path = Path(db_path)
    version = user_version(conn)
    result: dict[str, Any] = {
        "from_version": version, "to_version": version, "levels": []
    }
    if version >= SCHEMA_VERSION:
        # Still make sure additive objects exist (they are all IF NOT EXISTS).
        conn.executescript(_EXTRA_DDL)
        conn.commit()
        return result

    populated = _has_factors_table(conn)

    if version < 2:
        if populated:
            _backup_database(path, log, version or 1)
        added_factors = _add_columns(conn, "factors", FACTOR_COLUMNS_V2)
        _add_columns(conn, "factor_features", FEATURE_COLUMNS)
        _add_columns(conn, "factor_combinations", COMBINATION_COLUMNS)
        conn.executescript(_EXTRA_DDL_V2)

        # Pure-SQL backfill. experiment_hash is defined identically to the
        # historical exact_hash (SHA256 over canonical expr + settings); the
        # parser-dependent signal/family/template hashes are filled in by
        # FactorRegistry._backfill_v2().
        if added_factors and "experiment_hash" in added_factors:
            conn.execute(
                "UPDATE factors SET experiment_hash = exact_hash "
                "WHERE experiment_hash IS NULL"
            )
        conn.execute("PRAGMA user_version = 2")
        result["levels"].append(2)
        log.info("registry migrated to schema v2 (factor +%d)", len(added_factors))

    if version < 3:
        if populated:
            # A v1 database jumping straight to v3 was already backed up above
            # (.v1.bak); do not take a second copy in the same upgrade.
            if 2 not in result["levels"]:
                _backup_database(path, log, 2)
        added_v3 = _add_columns(conn, "factors", FACTOR_COLUMNS_V3)
        conn.executescript(_EXTRA_DDL_V3)
        # expression_hash requires SHA256 of the canonical expression, and
        # signal_hash must be recomputed to include region/universe/delay (v2
        # stored an expression-only hash). SQLite computes neither portably, so
        # FactorRegistry._backfill_v3() performs the Python backfill.
        conn.execute("PRAGMA user_version = 3")
        result["levels"].append(3)
        log.info("registry migrated to schema v3 (factor +%d)", len(added_v3))

    if version < 4:
        if populated:
            if 2 not in result["levels"] and 3 not in result["levels"]:
                _backup_database(path, log, 3)
        added_v4 = _add_columns(conn, "factors", FACTOR_COLUMNS_V4)
        conn.executescript(_EXTRA_DDL_V4)
        conn.execute("PRAGMA user_version = 4")
        result["levels"].append(4)
        log.info("registry migrated to schema v4 (factor +%d)", len(added_v4))

    if version < 5:
        if populated:
            if not result["levels"]:
                _backup_database(path, log, 4)
        added_v5 = _add_columns(conn, "factors", FACTOR_COLUMNS_V5)
        if added_v5:
            # Backfill evidence notes for rows finalized under v4: the note
            # describes the evidence shape actually on file.
            conn.execute(
                """
                UPDATE factors SET corr_evidence_note = 'SELF_NEIGHBORS'
                 WHERE corr_evidence_final_at IS NOT NULL
                   AND corr_evidence_note IS NULL
                   AND EXISTS (
                       SELECT 1 FROM factor_correlations c
                        WHERE c.factor_id = factors.id
                          AND c.correlation_type = 'SELF')
                """
            )
            conn.execute(
                """
                UPDATE factors SET corr_evidence_note = 'SELF_PASS'
                 WHERE corr_evidence_final_at IS NOT NULL
                   AND corr_evidence_note IS NULL
                """
            )
        conn.execute("PRAGMA user_version = 5")
        result["levels"].append(5)
        log.info("registry migrated to schema v5 (factor +%d)", len(added_v5))

    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()
    result["to_version"] = SCHEMA_VERSION
    return result
