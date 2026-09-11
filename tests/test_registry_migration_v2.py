"""v1 -> v2 registry migration tests.

A hand-built database with the *old* schema proves that upgrading:

* takes a one-time ``.v1.bak`` safety copy (never overwritten),
* keeps every historical row, metric, correlation and brain alpha id,
* backfills the new identity hashes,
* is idempotent on the second open.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from worldquant.registry import FactorRegistry, FactorStatus
from worldquant.registry.config import RegistryConfig
from worldquant.registry.store import CORR_TYPE_SELF


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Minimal-but-faithful v1 table set: every pre-v2 column, none of the 15 new
# factors columns / 3 feature columns / 5 combo columns / subtrees / meta.
_V1_SCHEMA = """
CREATE TABLE factors (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    exact_hash           TEXT    NOT NULL UNIQUE,
    expression           TEXT    NOT NULL,
    canonical_expression TEXT    NOT NULL,
    template_hash        TEXT    NOT NULL,
    structure_hash       TEXT    NOT NULL,
    field_template       TEXT,
    family_template      TEXT,
    status               TEXT    NOT NULL,
    region               TEXT,
    universe             TEXT,
    delay                INTEGER,
    decay                INTEGER,
    neutralization       TEXT,
    truncation           REAL,
    settings_json        TEXT,
    created_at           TEXT    NOT NULL,
    simulated_at         TEXT,
    submitted_at         TEXT,
    brain_alpha_id       TEXT UNIQUE,
    brain_simulation_id  TEXT,
    source               TEXT    NOT NULL DEFAULT 'generator',
    parent_factor_id     INTEGER,
    rejection_reason     TEXT,
    quality_score        REAL,
    novelty_score        REAL
);

CREATE TABLE factor_metrics (
    factor_id      INTEGER PRIMARY KEY REFERENCES factors(id) ON DELETE CASCADE,
    sharpe         REAL,
    fitness        REAL,
    returns        REAL,
    turnover       REAL,
    drawdown       REAL,
    margin         REAL,
    long_count     INTEGER,
    short_count    INTEGER,
    grade          TEXT,
    test_sharpe    REAL,
    test_fitness   REAL,
    checks_passed  INTEGER,
    checks_total   INTEGER,
    raw_metrics_json TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE factor_correlations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id          INTEGER NOT NULL REFERENCES factors(id) ON DELETE CASCADE,
    other_factor_id    INTEGER REFERENCES factors(id) ON DELETE SET NULL,
    other_brain_alpha_id TEXT,
    correlation        REAL,
    abs_correlation    REAL,
    correlation_type   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    other_key TEXT GENERATED ALWAYS AS (
        COALESCE(other_brain_alpha_id, 'L' || other_factor_id, 'X')
    ) STORED,
    UNIQUE(factor_id, other_key, correlation_type)
);

CREATE TABLE factor_features (
    factor_id            INTEGER PRIMARY KEY REFERENCES factors(id) ON DELETE CASCADE,
    operators_json       TEXT,
    fields_json          TEXT,
    windows_json         TEXT,
    operator_count       INTEGER,
    field_count          INTEGER,
    tree_depth           INTEGER,
    root_operator        TEXT,
    factor_family        TEXT,
    field_family         TEXT,
    assigned_locals_json TEXT,
    combination_keys_json TEXT,
    feature_json         TEXT
);

CREATE TABLE factor_combinations (
    combination_key        TEXT PRIMARY KEY,
    family_a               TEXT,
    family_b               TEXT,
    combine_operator       TEXT,
    trial_count            INTEGER NOT NULL DEFAULT 0,
    simulation_pass_count  INTEGER NOT NULL DEFAULT 0,
    corr_pass_count        INTEGER NOT NULL DEFAULT 0,
    submission_count       INTEGER NOT NULL DEFAULT 0,
    sharpe_sum             REAL,
    sharpe_n               INTEGER NOT NULL DEFAULT 0,
    best_sharpe            REAL,
    fitness_sum            REAL,
    fitness_n              INTEGER NOT NULL DEFAULT 0,
    best_fitness           REAL,
    abs_corr_sum           REAL,
    abs_corr_n             INTEGER NOT NULL DEFAULT 0,
    best_factor_id         INTEGER,
    updated_at             TEXT NOT NULL
);
"""


def _build_v1_database(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V1_SCHEMA)
        now = _iso()
        factors = [
            (
                "hash-a", "rank(ts_delta(close,5))", "rank(ts_delta(close,5))",
                "tpl-a", "str-a", "rank(ts_delta(<FIELD>,<WINDOW>))",
                "rank(ts_delta(PRICE,<WINDOW>))",
                FactorStatus.SIMULATED, "USA", "TOP3000", 1, 0, "INDUSTRY", 0.08,
                '{"region":"USA","universe":"TOP3000","delay":1}', now, now, None,
                None, None, "generator", None, None, 61.0, 70.0,
            ),
            (
                "hash-b", "rank(vwap)", "rank(vwap)",
                "tpl-b", "str-b", "rank(<FIELD>)", "rank(PRICE)",
                FactorStatus.CORR_REJECTED, "USA", "TOP3000", 1, 0, "INDUSTRY", 0.08,
                '{"region":"USA","universe":"TOP3000","delay":1}', now, now, None,
                "BRA-9001", None, "generator", None, "self corr 0.91", 80.0, 40.0,
            ),
            (
                "hash-c", "rank(volume)", "rank(volume)",
                "tpl-c", "str-c", "rank(<FIELD>)", "rank(LIQUIDITY)",
                FactorStatus.SUBMITTED, "USA", "TOP3000", 1, 0, "INDUSTRY", 0.08,
                '{"region":"USA","universe":"TOP3000","delay":1}', now, now, now,
                "BRA-9002", "sim-9002", "importer", None, None, 75.0, 60.0,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO factors (
                exact_hash, expression, canonical_expression, template_hash,
                structure_hash, field_template, family_template, status, region,
                universe, delay, decay, neutralization, truncation, settings_json,
                created_at, simulated_at, submitted_at, brain_alpha_id,
                brain_simulation_id, source, parent_factor_id, rejection_reason,
                quality_score, novelty_score
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            factors,
        )
        conn.execute(
            """
            INSERT INTO factor_metrics (
                factor_id, sharpe, fitness, returns, turnover, drawdown, margin,
                long_count, short_count, grade, test_sharpe, test_fitness,
                checks_passed, checks_total, raw_metrics_json, updated_at
            ) VALUES (2, 1.71, 1.52, 0.11, 0.3, -0.07, 0.0012, 120, 110,
                      'GOOD', 1.6, 1.4, 8, 8, '{}', ?)
            """,
            (now,),
        )
        # Local neighbor, an orphan BRAIN id not present in factors, and an
        # aggregate SELF_MAX row.
        conn.execute(
            """
            INSERT INTO factor_correlations (
                factor_id, other_factor_id, other_brain_alpha_id, correlation,
                abs_correlation, correlation_type, created_at
            ) VALUES (2, 3, NULL, 0.91, 0.91, ?, ?),
                     (2, NULL, 'BRAIN-ORPHAN', 0.88, 0.88, ?, ?),
                     (2, NULL, NULL, 0.91, 0.91, 'SELF_MAX', ?)
            """,
            (CORR_TYPE_SELF, now, CORR_TYPE_SELF, now, now),
        )
        conn.execute(
            """
            INSERT INTO factor_features (
                factor_id, operators_json, fields_json, windows_json,
                operator_count, field_count, tree_depth, root_operator,
                factor_family, field_family, assigned_locals_json,
                combination_keys_json, feature_json
            ) VALUES (1, '["rank","ts_delta"]', '["close"]', '[5]', 2, 1, 2,
                      'rank', 'PRICE_REVERSAL', 'PRICE', '[]', '[]', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO factor_combinations (
                combination_key, family_a, family_b, combine_operator,
                trial_count, simulation_pass_count, corr_pass_count,
                submission_count, sharpe_sum, sharpe_n, best_sharpe,
                fitness_sum, fitness_n, best_fitness, abs_corr_sum, abs_corr_n,
                best_factor_id, updated_at
            ) VALUES ('PRICE|ADD|LIQUIDITY', 'PRICE', 'LIQUIDITY', 'ADD', 3, 2, 1,
                      1, 4.5, 3, 1.8, 3.6, 3, 1.3, 0.8, 1, 3, ?)
            """,
            (now,),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def v1_db(tmp_path):
    path = tmp_path / "factor_registry.db"
    _build_v1_database(path)
    return path


def test_v1_to_v2_preserves_history_and_backfills(v1_db):
    backup = v1_db.with_name("factor_registry.db.v1.bak")
    assert not backup.exists()

    with FactorRegistry(v1_db, RegistryConfig()) as registry:
        stats = registry.stats()
        assert stats["total"] == 3
        rows = registry._query(
            "SELECT id, status, brain_alpha_id, signal_hash, experiment_hash, "
            "family_template_hash, high_quality_redundant FROM factors ORDER BY id"
        )
        by_id = {row["id"]: row for row in rows}
        assert by_id[1]["status"] == FactorStatus.SIMULATED
        assert by_id[2]["status"] == FactorStatus.CORR_REJECTED
        assert by_id[3]["status"] == FactorStatus.SUBMITTED
        assert by_id[3]["brain_alpha_id"] == "BRA-9002"
        for row in rows:
            assert row["signal_hash"]
            assert row["experiment_hash"] == "hash-a" if row["id"] == 1 else True
            assert row["family_template_hash"]
        # experiment_hash is the historical exact_hash (pure SQL backfill).
        assert by_id[2]["experiment_hash"] == "hash-b"

        metrics = registry.get_metrics(2)
        assert metrics["sharpe"] == pytest.approx(1.71)
        assert metrics["checks_passed"] == 8

        corr_rows = registry._query(
            "SELECT other_brain_alpha_id, correlation FROM factor_correlations "
            "WHERE factor_id = 2 ORDER BY id"
        )
        assert len(corr_rows) == 3
        assert {row["other_brain_alpha_id"] for row in corr_rows} == {
            None, "BRAIN-ORPHAN", None
        }

        features = registry._query(
            "SELECT fields_json, operator_paths_json FROM factor_features "
            "WHERE factor_id = 1"
        )[0]
        assert features["fields_json"] == '["close"]'
        assert features["operator_paths_json"]  # backfilled by _backfill_v2

    assert backup.exists()


def test_migration_is_idempotent_and_never_overwrites_backup(v1_db):
    backup = v1_db.with_name("factor_registry.db.v1.bak")
    with FactorRegistry(v1_db, RegistryConfig()):
        pass
    assert backup.exists()
    first_size = backup.stat().st_size

    # Second open must not rewrite the backup or change user_version/data.
    with FactorRegistry(v1_db, RegistryConfig()) as registry:
        assert registry.stats()["total"] == 3
        meta = registry._query(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        )
        assert meta and meta[0]["value"] == "2"

    assert backup.exists()
    assert backup.stat().st_size == first_size
    raw = sqlite3.connect(str(v1_db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        assert raw.execute("SELECT COUNT(*) FROM factors").fetchone()[0] == 3
    finally:
        raw.close()
