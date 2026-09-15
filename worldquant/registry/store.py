"""The Factor Registry: SQLite-backed memory of every researched alpha.

Unlike :class:`~worldquant.storage.ResultStore`, which tracks *simulation
executions*, the registry tracks *research attempts* keyed by expression
fingerprints, and deliberately keeps failures, duplicates and correlation
rejections forever so the same direction is never researched twice.

The schema (``factors`` / ``factor_metrics`` / ``factor_correlations`` /
``factor_features`` / ``factor_combinations``) is additive and created with
``IF NOT EXISTS``; a separate database file (``data/factor_registry.db``) is
used so the existing, proven backtest pipeline is untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..api import normalize_settings
from ..exceptions import StorageError
from ..hashing import (
    _HASH_SEPARATOR,
    dedup_key,
    experiment_identity,
    expression_hash,
    expression_identity,
    normalize_expression,
    signal_identity,
)
from ..logging_utils import get_logger
from ..storage import utcnow_iso
from .combinations import Combination, extract_combinations
from .config import RegistryConfig
from .correlation import (
    CorrelationBand,
    CorrelationDecision,
    CorrelationGate,
    CorrelationStatus,
)
from .expr_parser import analyze_expression, parse_expression_ast
from .failures import (
    DUPLICATE as _FAILURE_DUPLICATE,
    SELF_CORRELATION as _FAILURE_SELF_CORRELATION,
    SIMULATION_ERROR as _FAILURE_SIM_ERROR,
    classify_failure,
)
from .families import FieldFamilyResolver, classify_factor_family
from .migrations import SCHEMA_VERSION, ensure_migrated
from .scoring import quality_score
from .similarity import jaccard, overall_similarity, window_set_similarity
from .themes import classify_theme

# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
class FactorStatus:
    """Unified alpha lifecycle. Every value is persisted, none are dropped."""

    GENERATED = "GENERATED"
    DUPLICATE = "DUPLICATE"
    SIMULATING = "SIMULATING"
    SIMULATED = "SIMULATED"
    METRIC_REJECTED = "METRIC_REJECTED"
    CORR_REJECTED = "CORR_REJECTED"
    PASSED = "PASSED"
    SUBMITTED = "SUBMITTED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    SIMULATION_FAILED = "SIMULATION_FAILED"

    #: Factors that actually consumed research effort — the research de-dup set.
    RESEARCH_SET: frozenset[str] = frozenset(
        {SIMULATED, METRIC_REJECTED, CORR_REJECTED, PASSED, SUBMITTED}
    )
    #: Factors that count as "we know what this expression does" — used by
    #: template saturation and exact-duplicate history.
    KNOWLEDGE_SET: frozenset[str] = RESEARCH_SET | frozenset(
        {SUBMIT_FAILED, SIMULATION_FAILED}
    )
    TERMINAL_SET: frozenset[str] = frozenset(
        {
            DUPLICATE, SIMULATED, METRIC_REJECTED, CORR_REJECTED,
            PASSED, SUBMITTED, SUBMIT_FAILED, SIMULATION_FAILED,
        }
    )


#: Correlation provenance types — never mixed into one semantic field.
CORR_TYPE_SELF = "SELF"          # per-neighbor rows from /alphas/{id}/check
CORR_TYPE_SELF_MAX = "SELF_MAX"  # only the worst value was available
CORR_TYPE_PRODUCTION = "PRODUCTION"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
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
    novelty_score        REAL,
    -- v2 identity layer -----------------------------------------------------
    signal_hash          TEXT,
    experiment_hash      TEXT,
    family_template_hash TEXT,
    ablation_group_id    TEXT,
    changed_parameters_json TEXT,
    parent_experiment_id INTEGER,
    high_quality_redundant INTEGER NOT NULL DEFAULT 0,
    failure_category     TEXT,
    corr_status          TEXT,
    corr_band            TEXT,
    corr_margin          REAL,
    nearest_cluster_id   INTEGER,
    theme                TEXT,
    branch               TEXT,
    subtheme             TEXT,
    -- v3 identity layer -----------------------------------------------------
    expression_hash      TEXT,
    corr_last_checked_at TEXT,
    corr_check_attempts  INTEGER NOT NULL DEFAULT 0,
    corr_error           TEXT,
    -- v4: final SELF_CORRELATION evidence obtained via GET /alphas/{id}/check
    corr_evidence_final_at TEXT,
    -- v5: why evidence is final (SELF_PASS/SELF_FAIL/SELF_NEIGHBORS/
    -- GATED:<checks>/ALREADY_SUBMITTED); NULL while still pending
    corr_evidence_note    TEXT
);

CREATE TABLE IF NOT EXISTS factor_metrics (
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

CREATE TABLE IF NOT EXISTS factor_correlations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id          INTEGER NOT NULL REFERENCES factors(id) ON DELETE CASCADE,
    other_factor_id    INTEGER REFERENCES factors(id) ON DELETE SET NULL,
    other_brain_alpha_id TEXT,
    correlation        REAL,
    abs_correlation    REAL,
    correlation_type   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    -- Stable neighbor key for upsert: a remote BRAIN alpha id wins; a local
    -- factor becomes 'L<id>'; an aggregate SELF_MAX row (no neighbor at all)
    -- becomes 'X', so exactly one such row can exist per (factor, type).
    other_key TEXT GENERATED ALWAYS AS (
        COALESCE(other_brain_alpha_id, 'L' || other_factor_id, 'X')
    ) STORED,
    UNIQUE(factor_id, other_key, correlation_type)
);

CREATE TABLE IF NOT EXISTS factor_features (
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
    feature_json         TEXT,
    operator_paths_json  TEXT,
    operator_multiset_json TEXT,
    field_families_json  TEXT
);

CREATE TABLE IF NOT EXISTS factor_combinations (
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
    updated_at             TEXT NOT NULL,
    left_template_hash     TEXT,
    right_template_hash    TEXT,
    left_structure_hash    TEXT,
    right_structure_hash   TEXT,
    structure_hash         TEXT
);

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

CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT
);

-- Field metadata cache, populated on demand from GET /data-fields/{id}.
-- Only fields actually used by stored factors are fetched; this table is
-- read-side enrichment (semantics / dataset / family) and never participates
-- in expression/signal/experiment identity hashing.
CREATE TABLE IF NOT EXISTS field_metadata (
    field_id        TEXT PRIMARY KEY,
    dataset_id      TEXT,
    dataset_name    TEXT,
    category_id     TEXT,
    category_name   TEXT,
    subcategory_id  TEXT,
    subcategory_name TEXT,
    description     TEXT,
    field_type      TEXT,
    visualizable    INTEGER,
    raw_json        TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_field_metadata_dataset ON field_metadata(dataset_id);
CREATE INDEX IF NOT EXISTS idx_field_metadata_category ON field_metadata(category_id);

CREATE INDEX IF NOT EXISTS idx_factors_template    ON factors(template_hash);
CREATE INDEX IF NOT EXISTS idx_factors_structure   ON factors(structure_hash);
CREATE INDEX IF NOT EXISTS idx_factors_status      ON factors(status);
CREATE INDEX IF NOT EXISTS idx_factors_brain_alpha ON factors(brain_alpha_id);
CREATE INDEX IF NOT EXISTS idx_features_family     ON factor_features(factor_family);
CREATE INDEX IF NOT EXISTS idx_corr_factor         ON factor_correlations(factor_id);
CREATE INDEX IF NOT EXISTS idx_corr_other          ON factor_correlations(other_factor_id);
CREATE INDEX IF NOT EXISTS idx_corr_brain_other    ON factor_correlations(other_brain_alpha_id);
-- v2-added indexes (signal/experiment/family-template hashes, failure/corr/
-- theme columns, factor_subtrees) live in migrations._EXTRA_DDL so they are
-- created only AFTER the additive ALTER TABLE on pre-v2 databases.
-- user_version for a brand-new database is set to SCHEMA_VERSION in __init__
-- (after detecting the table was absent), so a pre-v2 file is not masked.
"""


@dataclass
class RegisteredCandidate:
    factor_id: int
    created: bool
    exact_hash: str
    status: str


@dataclass
class SimilarFactor:
    factor_id: int
    expression: str
    status: str
    similarity: float
    factor_family: str | None = None
    brain_alpha_id: str | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dumps(payload: Any) -> str:
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


class FactorRegistry:
    """Thread-safe SQLite registry + fingerprint/gate/correlation services."""

    def __init__(
        self,
        db_path: str | Path,
        config: RegistryConfig | None = None,
        *,
        field_datasets: dict[str, str] | None = None,
        logger: Any = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config or RegistryConfig()
        self.log = logger or get_logger("registry")
        self.resolver = FieldFamilyResolver(field_datasets=dict(field_datasets or {}))
        self._lock = threading.RLock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path), timeout=30.0, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open registry at {self.db_path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                pre_existing = bool(
                    self._conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='factors'"
                    ).fetchone()
                )
                self._conn.executescript(_SCHEMA)
                if not pre_existing:
                    # Brand-new database: _SCHEMA already creates the full v2
                    # shape, so stamp it directly and skip backup/migration.
                    self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._conn.commit()
                migration = ensure_migrated(self._conn, self.db_path, self.log)
                levels = migration.get("levels") or []
                if 2 in levels:
                    self._backfill_v2()
                else:
                    # Cheap repair path for interrupted v1->v2 upgrades.
                    if self._query(
                        "SELECT COUNT(*) AS n FROM factors WHERE signal_hash IS NULL"
                    )[0]["n"]:
                        self._backfill_v2()
                if 3 in levels:
                    self._backfill_v3()
                elif self._query(
                    "SELECT COUNT(*) AS n FROM factors WHERE expression_hash IS NULL"
                )[0]["n"]:
                    # Repair path for an interrupted v2->v3 upgrade.
                    self._backfill_v3()
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot initialize registry schema: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "FactorRegistry":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor
        except sqlite3.Error as exc:
            raise StorageError(
                f"registry write failed: {exc} | sql={sql.strip()[:120]}"
            ) from exc

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            with self._lock:
                return list(self._conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise StorageError(
                f"registry read failed: {exc} | sql={sql.strip()[:120]}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Fingerprints
    # ------------------------------------------------------------------ #
    def fingerprints(
        self, expression: str, settings: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Compute every fingerprint + feature for an expression/settings pair."""
        normalized = normalize_settings(settings)
        scope = _HASH_SEPARATOR.join(
            str(normalized.get(key, "")) for key in ("region", "universe", "delay")
        )
        exact = dedup_key(expression, normalized)
        canonical = normalize_expression(expression)
        features = analyze_expression(canonical, family_resolver=self.resolver)

        families = [self.resolver(name) for name in features.fields]
        primary_family = max(set(families), key=families.count) if families else None
        factor_family = classify_factor_family(
            features.fields, features.operators, resolver=self.resolver
        )
        combinations = self._combinations_for(canonical)
        # Unified three-layer identity (worldquant.hashing is the single
        # source of truth shared with ResultStore):
        #   expression — mathematical expression alone
        #   signal     — expression + region/universe/delay (information set)
        #   experiment — expression + every normalized setting
        expression_id = expression_identity(canonical)
        signal_id = signal_identity(canonical, normalized)
        experiment_id = experiment_identity(canonical, normalized)

        return {
            "exact_hash": exact,
            "experiment_hash": experiment_id,
            "signal_hash": signal_id,
            "expression_hash": expression_id,
            "canonical": canonical,
            "template_hash": _sha256(features.template + _HASH_SEPARATOR + scope),
            "structure_hash": _sha256(features.structure),
            "family_template_hash": _sha256(
                features.family_template + _HASH_SEPARATOR + scope
            ),
            "field_template": features.field_template,
            "family_template": features.family_template,
            "features": features,
            "field_family": primary_family,
            "field_families": families,
            "factor_family": factor_family,
            "combinations": combinations,
            "scope_settings": {
                "region": normalized.get("region"),
                "universe": normalized.get("universe"),
                "delay": normalized.get("delay"),
                "decay": normalized.get("decay"),
                "neutralization": normalized.get("neutralization"),
                "truncation": normalized.get("truncation"),
            },
            "settings": normalized,
        }

    def _combinations_for(self, canonical: str) -> list[Combination]:
        try:
            statements, assigned = parse_expression_ast(canonical)
        except Exception:  # noqa: BLE001 - combination parsing is best-effort
            return []
        try:
            return extract_combinations(
                statements, assigned, resolver=self.resolver
            )
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ #
    # Candidate registration
    # ------------------------------------------------------------------ #
    def register_candidate(
        self,
        expression: str,
        settings: dict[str, Any] | None = None,
        *,
        source: str = "generator",
        parent_factor_id: int | None = None,
        brain_alpha_id: str | None = None,
        brain_simulation_id: str | None = None,
        ablation_group_id: str | None = None,
        changed_parameters: dict[str, Any] | None = None,
        parent_experiment_id: int | None = None,
    ) -> RegisteredCandidate:
        """Insert a GENERATED candidate, or return the existing factor. Idempotent."""
        fp = self.fingerprints(expression, settings)
        exact_hash = fp["exact_hash"]

        # A BRAIN alpha id is the authoritative identity for a simulated or
        # submitted alpha: when one is supplied, prefer the row that already
        # carries it over an exact-hash match. Otherwise importing a live
        # alpha whose expression has drifted (or a re-run that landed on a
        # different exact row) tries to set a brain_alpha_id already owned by
        # another factor and violates the UNIQUE constraint.
        if brain_alpha_id:
            linked = self._query(
                "SELECT id, status FROM factors WHERE brain_alpha_id = ?",
                (brain_alpha_id,),
            )
            if linked:
                return RegisteredCandidate(
                    factor_id=int(linked[0]["id"]), created=False,
                    exact_hash=exact_hash, status=str(linked[0]["status"]),
                )

        existing = self._query(
            "SELECT id, status FROM factors WHERE exact_hash = ?", (exact_hash,)
        )
        if existing:
            return RegisteredCandidate(
                factor_id=int(existing[0]["id"]),
                created=False,
                exact_hash=exact_hash,
                status=str(existing[0]["status"]),
            )

        scope = fp["scope_settings"]
        now = utcnow_iso()
        theme = classify_theme(
            fp["features"].fields,
            fp["features"].operators,
            resolver=self.resolver,
        )
        theme_parts = (theme or "").split(".")
        cursor = self._execute(
            """
            INSERT INTO factors (
                exact_hash, expression, canonical_expression,
                template_hash, structure_hash, field_template, family_template,
                status, region, universe, delay, decay, neutralization, truncation,
                settings_json, created_at, brain_alpha_id, brain_simulation_id,
                source, parent_factor_id,
                signal_hash, experiment_hash, family_template_hash,
                ablation_group_id, changed_parameters_json, parent_experiment_id,
                theme, branch, subtheme, expression_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exact_hash, expression, fp["canonical"],
                fp["template_hash"], fp["structure_hash"],
                fp["field_template"], fp["family_template"],
                FactorStatus.GENERATED,
                scope.get("region"), scope.get("universe"), scope.get("delay"),
                scope.get("decay"), scope.get("neutralization"), scope.get("truncation"),
                _dumps(fp["settings"]), now,
                brain_alpha_id or None, brain_simulation_id or None,
                source, parent_factor_id,
                fp["signal_hash"], fp["experiment_hash"], fp["family_template_hash"],
                ablation_group_id,
                _dumps(changed_parameters) if changed_parameters else None,
                parent_experiment_id,
                theme_parts[0] if theme_parts else None,
                theme_parts[1] if len(theme_parts) > 1 else None,
                theme_parts[2] if len(theme_parts) > 2 else None,
                fp["expression_hash"],
            ),
        )
        factor_id = int(cursor.lastrowid)
        self._save_features(factor_id, fp)
        self._save_subtrees(factor_id, fp)
        self._record_combo_legs(factor_id, fp)
        return RegisteredCandidate(
            factor_id=factor_id, created=True, exact_hash=exact_hash,
            status=FactorStatus.GENERATED,
        )

    def _save_features(self, factor_id: int, fp: dict[str, Any]) -> None:
        f = fp["features"]
        combo_keys = [combo.key for combo in fp["combinations"]]
        feature_json = f.to_feature_json()
        field_families = fp.get("field_families") or [
            self.resolver(name) for name in f.fields
        ]
        feature_json["field_families"] = field_families
        self._execute(
            """
            INSERT OR REPLACE INTO factor_features (
                factor_id, operators_json, fields_json, windows_json,
                operator_count, field_count, tree_depth, root_operator,
                factor_family, field_family, assigned_locals_json,
                combination_keys_json, feature_json,
                operator_paths_json, operator_multiset_json, field_families_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                factor_id, _dumps(f.operators), _dumps(f.fields), _dumps(f.windows),
                f.operator_count, f.field_count, f.tree_depth, f.root_operator,
                fp["factor_family"], fp["field_family"],
                _dumps(f.assigned_locals), _dumps(combo_keys), _dumps(feature_json),
                _dumps(f.operator_paths), _dumps(f.operator_multiset),
                _dumps(field_families),
            ),
        )

    def _save_subtrees(self, factor_id: int, fp: dict[str, Any]) -> None:
        """Persist per-leg fingerprints of every blend/group subtree."""
        self._execute(
            "DELETE FROM factor_subtrees WHERE factor_id = ?", (factor_id,)
        )
        subtrees = fp["features"].subtrees or []
        for subtree_index, subtree in enumerate(subtrees):
            for leg in subtree.get("legs", []):
                leg_fields = leg.get("fields") or []
                leg_families = [self.resolver(name) for name in leg_fields]
                leg_family = (
                    max(set(leg_families), key=leg_families.count)
                    if leg_families else "UNKNOWN"
                )
                self._execute(
                    """
                    INSERT OR REPLACE INTO factor_subtrees (
                        factor_id, subtree_index, node_label, leg_index,
                        leg_family, leg_template_hash, leg_structure_hash,
                        leg_template, leg_structure
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        factor_id, subtree_index, subtree.get("node"),
                        leg.get("index", 0), leg_family,
                        _sha256(leg.get("template", "")),
                        _sha256(leg.get("structure", "")),
                        leg.get("template"), leg.get("structure"),
                    ),
                )

    def _record_combo_legs(self, factor_id: int, fp: dict[str, Any]) -> None:
        """Attach this factor's per-leg hashes to its combination rows."""
        # Map node label -> ordered leg (template_hash, structure_hash).
        legs_by_node: dict[str, list[dict[str, str]]] = {}
        for subtree in fp["features"].subtrees or []:
            node = str(subtree.get("node", "")).lower()
            legs_by_node.setdefault(node, []).append(
                {
                    "template_hash": _sha256(
                        subtree["legs"][0].get("template", "")
                        if subtree.get("legs") else ""
                    ),
                    "structure_hash": _sha256(
                        subtree["legs"][0].get("structure", "")
                        if subtree.get("legs") else ""
                    ),
                }
            )
        rows = self._query(
            "SELECT combination_key FROM factor_combinations"
        )
        existing = {row["combination_key"] for row in rows}
        for combo in fp["combinations"]:
            if combo.key not in existing:
                continue
            left = getattr(combo, "left_leg", None)
            right = getattr(combo, "right_leg", None)
            if left is None and right is None:
                continue
            self._execute(
                """
                UPDATE factor_combinations SET
                    left_template_hash = COALESCE(left_template_hash, ?),
                    right_template_hash = COALESCE(right_template_hash, ?),
                    left_structure_hash = COALESCE(left_structure_hash, ?),
                    right_structure_hash = COALESCE(right_structure_hash, ?),
                    structure_hash = COALESCE(structure_hash, ?)
                WHERE combination_key = ?
                """,
                (
                    left.get("template_hash") if left else None,
                    right.get("template_hash") if right else None,
                    left.get("structure_hash") if left else None,
                    right.get("structure_hash") if right else None,
                    combo.structure_hash if getattr(combo, "structure_hash", None) else None,
                    combo.key,
                ),
            )

    # ------------------------------------------------------------------ #
    # Status transitions
    # ------------------------------------------------------------------ #
    def set_status(
        self,
        factor_id: int,
        status: str,
        *,
        rejection_reason: str | None = None,
        brain_alpha_id: str | None = None,
        brain_simulation_id: str | None = None,
    ) -> None:
        assignments = ["status = ?"]
        params: list[Any] = [status]
        if rejection_reason is not None:
            assignments.append("rejection_reason = ?")
            params.append(rejection_reason)
        if brain_alpha_id is not None:
            assignments.append("brain_alpha_id = ?")
            params.append(brain_alpha_id)
        if brain_simulation_id is not None:
            assignments.append("brain_simulation_id = ?")
            params.append(brain_simulation_id)
        if status in (FactorStatus.SIMULATED, FactorStatus.PASSED,
                      FactorStatus.SUBMITTED, FactorStatus.CORR_REJECTED,
                      FactorStatus.METRIC_REJECTED, FactorStatus.SIMULATION_FAILED):
            # SIMULATION_FAILED is included so re-migrations can tell a failed
            # run was already processed (simulated_at doubles as "processed_at").
            assignments.append("simulated_at = COALESCE(simulated_at, ?)")
            params.append(utcnow_iso())
        if status == FactorStatus.SUBMITTED:
            assignments.append("submitted_at = COALESCE(submitted_at, ?)")
            params.append(utcnow_iso())
        params.append(factor_id)
        self._execute(
            f"UPDATE factors SET {', '.join(assignments)} WHERE id = ?", params
        )

    def mark_duplicate(self, factor_id: int, reason: str) -> None:
        self.set_status(factor_id, FactorStatus.DUPLICATE, rejection_reason=reason)

    def mark_simulation_failed(self, factor_id: int, reason: str) -> None:
        self.set_status(
            factor_id, FactorStatus.SIMULATION_FAILED, rejection_reason=reason
        )

    def mark_metric_rejected(self, factor_id: int, reasons: str | list[str]) -> None:
        reason = reasons if isinstance(reasons, str) else "; ".join(reasons)
        self.set_status(
            factor_id, FactorStatus.METRIC_REJECTED, rejection_reason=reason
        )

    def mark_corr_rejected(self, factor_id: int, decision: CorrelationDecision) -> None:
        self.set_status(
            factor_id, FactorStatus.CORR_REJECTED, rejection_reason=decision.reason
        )
        self._execute(
            """
            UPDATE factors SET
                failure_category = ?,
                corr_status = ?,
                corr_band = ?,
                corr_margin = ?,
                high_quality_redundant = ?
            WHERE id = ?
            """,
            (
                _FAILURE_SELF_CORRELATION,
                CorrelationStatus.FAIL.value,
                decision.band.value if decision.band else None,
                decision.corr_margin,
                1 if self._is_high_quality(factor_id) else 0,
                factor_id,
            ),
        )

    def _is_high_quality(self, factor_id: int) -> bool:
        """A corr-rejected factor may still be a strong, worth-keeping idea."""
        memory = self.config.memory
        metrics = self.get_metrics(factor_id) or {}
        sharpe = metrics.get("sharpe")
        fitness = metrics.get("fitness")
        if sharpe is None or fitness is None:
            return False
        return (
            float(sharpe) >= memory.high_quality_min_sharpe
            and float(fitness) >= memory.high_quality_min_fitness
        )

    def get_high_quality_redundant(
        self, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Strong signals that lost only to correlation — keep idea, switch data."""
        rows = self._query(
            """
            SELECT f.id, f.expression, f.family_template, f.corr_margin,
                   f.rejection_reason, m.sharpe, m.fitness, m.turnover
            FROM factors f LEFT JOIN factor_metrics m ON m.factor_id = f.id
            WHERE f.high_quality_redundant = 1
            ORDER BY COALESCE(m.fitness, 0) DESC, COALESCE(m.sharpe, 0) DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def mark_submitted(
        self, factor_id: int, *, brain_alpha_id: str | None = None
    ) -> None:
        previous = self.get_factor(factor_id)
        self.set_status(
            factor_id, FactorStatus.SUBMITTED, brain_alpha_id=brain_alpha_id
        )
        # Guard against double counting on idempotent re-imports: a factor that
        # was already SUBMITTED, or already carried a submitted_at timestamp,
        # must not inflate the combination submission counters.
        already_submitted = previous is not None and (
            previous["status"] == FactorStatus.SUBMITTED
            or previous["submitted_at"] is not None
        )
        if not already_submitted:
            self._bump_combo_counters(factor_id, submission_delta=1)

    def mark_submit_failed(self, factor_id: int, reason: str) -> None:
        self.set_status(
            factor_id, FactorStatus.SUBMIT_FAILED, rejection_reason=reason
        )

    # ------------------------------------------------------------------ #
    # Simulation results (accepts the existing worldquant.AlphaResult)
    # ------------------------------------------------------------------ #
    def save_simulation_result(self, result: Any) -> int:
        """Persist a completed/failed run from an ``AlphaResult``-like object.

        Reads only duck-typed attributes, so both
        :class:`worldquant.models.AlphaResult` and plain import dicts work.
        """
        expression = str(getattr(result, "expression", "") or "")
        settings = _loads(getattr(result, "settings_json", "{}"), {})
        brain_alpha_id = getattr(result, "remote_alpha_id", None)
        brain_simulation_id = getattr(result, "simulation_id", None)
        candidate = self.register_candidate(
            expression, settings,
            source=getattr(result, "registry_source", "generator"),
            brain_alpha_id=brain_alpha_id,
            brain_simulation_id=brain_simulation_id,
        )
        factor_id = candidate.factor_id
        factor_before = self.get_factor(factor_id)
        first_completion = factor_before is not None and not factor_before["simulated_at"]

        status = str(getattr(result, "status", "") or "")
        if status != "COMPLETED":
            reason = getattr(result, "error", None) or f"simulation status={status}"
            self.set_status(
                factor_id, FactorStatus.SIMULATION_FAILED,
                rejection_reason=str(reason)[:500],
                brain_alpha_id=brain_alpha_id,
                brain_simulation_id=brain_simulation_id,
            )
            self._execute(
                "UPDATE factors SET failure_category = ? WHERE id = ?",
                (_FAILURE_SIM_ERROR, factor_id),
            )
            if first_completion:
                self._bump_combo_counters(factor_id, trial_delta=1)
            return factor_id

        raw_metrics = {
            key: getattr(result, key, None)
            for key in (
                "sharpe", "fitness", "turnover", "returns", "drawdown", "margin",
                "pnl", "book_size", "long_count", "short_count", "grade", "stage",
            )
        }
        checks = getattr(result, "submission_checks", None) or getattr(result, "checks", None) or {}
        from ..api import SUBMISSION_CHECKS, passed_check_count

        checks_passed = passed_check_count(checks) if checks else None
        test_stats = getattr(result, "test_stats", None) or {}
        self._execute(
            """
            INSERT OR REPLACE INTO factor_metrics (
                factor_id, sharpe, fitness, returns, turnover, drawdown, margin,
                long_count, short_count, grade, test_sharpe, test_fitness,
                checks_passed, checks_total, raw_metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                factor_id, getattr(result, "sharpe", None),
                getattr(result, "fitness", None), getattr(result, "returns", None),
                getattr(result, "turnover", None), getattr(result, "drawdown", None),
                getattr(result, "margin", None),
                getattr(result, "long_count", None), getattr(result, "short_count", None),
                getattr(result, "grade", None),
                test_stats.get("sharpe") if isinstance(test_stats, dict) else None,
                test_stats.get("fitness") if isinstance(test_stats, dict) else None,
                checks_passed, len(SUBMISSION_CHECKS) if checks else None,
                _dumps(raw_metrics), utcnow_iso(),
            ),
        )

        score = quality_score(
            sharpe=getattr(result, "sharpe", None),
            fitness=getattr(result, "fitness", None),
            turnover=getattr(result, "turnover", None),
            drawdown=getattr(result, "drawdown", None),
        )
        self._execute(
            "UPDATE factors SET quality_score = ?, brain_alpha_id = COALESCE(brain_alpha_id, ?), "
            "brain_simulation_id = COALESCE(brain_simulation_id, ?) WHERE id = ?",
            (score, brain_alpha_id, brain_simulation_id, factor_id),
        )

        metric_passed = bool(getattr(result, "passed", False))
        # Initial post-simulation status; the correlation gate may move it on.
        if metric_passed:
            self.set_status(factor_id, FactorStatus.SIMULATED)
        else:
            reasons_list = getattr(result, "reasons", None) or [
                "did not pass configured metric filters"
            ]
            self.mark_metric_rejected(factor_id, list(reasons_list))
            memory = self.config.memory
            assessment = classify_failure(
                status=FactorStatus.METRIC_REJECTED,
                checks=checks,
                reasons=[str(r) for r in reasons_list],
                sharpe=getattr(result, "sharpe", None),
                fitness=getattr(result, "fitness", None),
                long_count=getattr(result, "long_count", None),
                short_count=getattr(result, "short_count", None),
                train_sharpe=getattr(result, "sharpe", None),
                test_sharpe=(
                    test_stats.get("sharpe") if isinstance(test_stats, dict) else None
                ),
                one_sided_ratio=memory.one_sided_book_ratio,
                overfit_retention=memory.overfit_test_retention,
                overfit_min_train_sharpe=memory.overfit_min_train_sharpe,
            )
            self._execute(
                "UPDATE factors SET failure_category = ? WHERE id = ?",
                (assessment.primary, factor_id),
            )

        if first_completion:
            self._bump_combo_counters(
                factor_id,
                trial_delta=1,
                metric_pass_delta=1 if metric_passed else 0,
            )
            # Fold Sharpe/Fitness into per-combination avg/best exactly once,
            # when the first completed metrics land (not on re-imports).
            self._record_combo_performance(factor_id)
            self._backfill_combo_legs([factor_id])
        return factor_id

    def handle_completed(
        self,
        result: Any,
        *,
        corr_records: list[dict[str, Any]] | None = None,
        apply_gate: bool = True,
    ) -> dict[str, Any]:
        """One-call adapter hook: metrics -> metric reject -> correlation gate.

        Returns ``{"factor_id", "status", "corr_status",
        "correlation_decision"}`` (an extra ``"error"`` key is added on the
        swallowed-exception path). ``corr_status`` is the persisted verdict:
        PASS/FAIL/UNKNOWN when the gate is enabled, NOT_APPLICABLE when the
        gate is disabled by config. Never raises: registry bookkeeping must
        not break a backtest run.
        """
        try:
            factor_id = self.save_simulation_result(result)
            factor = self.get_factor(factor_id)
            decision: CorrelationDecision | None = None
            if factor and factor["status"] in (
                FactorStatus.SIMULATED, FactorStatus.PASSED
            ):
                records = list(corr_records or [])
                max_self = getattr(result, "self_correlation", None)
                if records:
                    # Edges feed the graph even when the gate itself is off.
                    self.save_correlations(factor_id, records, CORR_TYPE_SELF)
                elif max_self is not None:
                    self.save_correlations(
                        factor_id,
                        [{"correlation": max_self, "other_brain_alpha_id": None}],
                        CORR_TYPE_SELF_MAX,
                    )
                if not apply_gate:
                    pass
                elif self.config.correlation.enabled and (records or max_self is not None):
                    decision = self.apply_correlation_gate(factor_id)
                elif self.config.correlation.enabled:
                    # Metric-passed but no correlation evidence at all: record
                    # UNKNOWN explicitly (factor stays SIMULATED, never PASSED).
                    self._execute(
                        "UPDATE factors SET corr_status = ?, corr_band = NULL, "
                        "corr_margin = NULL WHERE id = ?",
                        (CorrelationStatus.UNKNOWN.value, factor_id),
                    )
                else:
                    # Local correlation gate disabled by config. This is NOT
                    # UNKNOWN: there is no pending evidence to wait for, so a
                    # metric-passed factor is promoted to PASSED and its corr
                    # verdict is recorded as NOT_APPLICABLE. BRAIN's official
                    # submission checks live separately on the metrics row.
                    # No corr-pass counter bump: the gate itself never ran.
                    self.set_status(factor_id, FactorStatus.PASSED)
                    self._execute(
                        "UPDATE factors SET corr_status = ?, corr_band = NULL, "
                        "corr_margin = NULL, failure_category = NULL WHERE id = ?",
                        (CorrelationStatus.NOT_APPLICABLE.value, factor_id),
                    )
            final = self.get_factor(factor_id)
            return {
                "factor_id": factor_id,
                "status": final["status"],
                "corr_status": final["corr_status"],
                "correlation_decision": decision,
            }
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self.log.warning("registry.handle_completed failed: %s", exc)
            return {"factor_id": None, "status": None, "corr_status": None,
                    "correlation_decision": None,
                    "error": str(exc)}

    # ------------------------------------------------------------------ #
    # Correlations
    # ------------------------------------------------------------------ #
    def save_correlations(
        self,
        factor_id: int,
        records: Iterable[dict[str, Any]],
        correlation_type: str = CORR_TYPE_SELF,
    ) -> int:
        """Store per-neighbor correlations. Idempotent per (factor, neighbor, type)."""
        written = 0
        for record in records:
            value = record.get("correlation")
            if value is None:
                value = record.get("self_correlation")
            if value is None:
                continue
            try:
                corr = float(value)
            except (TypeError, ValueError):
                continue
            other_brain = (
                record.get("other_brain_alpha_id")
                or record.get("alpha_id")
                or record.get("other_alpha_id")
            )
            other_brain = str(other_brain) if other_brain else None
            other_factor_row = (
                self._query(
                    "SELECT id FROM factors WHERE brain_alpha_id = ?", (other_brain,)
                ) if other_brain else []
            )
            other_factor_id = int(other_factor_row[0]["id"]) if other_factor_row else None
            self._execute(
                """
                INSERT INTO factor_correlations (
                    factor_id, other_factor_id, other_brain_alpha_id,
                    correlation, abs_correlation, correlation_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_id, other_key, correlation_type)
                DO UPDATE SET correlation=excluded.correlation,
                               abs_correlation=excluded.abs_correlation,
                               other_factor_id=COALESCE(excluded.other_factor_id,
                                   factor_correlations.other_factor_id),
                               other_brain_alpha_id=COALESCE(excluded.other_brain_alpha_id,
                                   factor_correlations.other_brain_alpha_id)
                """,
                (
                    factor_id, other_factor_id, other_brain, corr, abs(corr),
                    correlation_type, utcnow_iso(),
                ),
            )
            written += 1
        return written

    def reconcile_correlation_neighbors(self) -> dict[str, int]:
        """Resolve stored neighbor BRAIN alpha ids to local factor ids.

        A simulation stored before the neighbor alpha itself was imported has
        ``other_brain_alpha_id`` but NULL ``other_factor_id``. Once the neighbor
        is imported later (brain_alpha_id known), this backfills the link. It
        is idempotent and safe to run after every import/refresh. Duplicate
        edges created by merging an orphan remote row with a newly-resolved
        local row are collapsed, keeping the strongest (highest |corr|).
        """
        resolved = self._execute(
            """
            UPDATE factor_correlations
               SET other_factor_id = (
                       SELECT f.id FROM factors f
                        WHERE f.brain_alpha_id =
                              factor_correlations.other_brain_alpha_id
                        LIMIT 1
                   )
             WHERE other_factor_id IS NULL
               AND other_brain_alpha_id IS NOT NULL
               AND EXISTS (
                       SELECT 1 FROM factors f
                        WHERE f.brain_alpha_id =
                              factor_correlations.other_brain_alpha_id
                   )
            """
        ).rowcount
        deduped = self._execute(
            """
            DELETE FROM factor_correlations
             WHERE id IN (
                 SELECT id FROM (
                     SELECT id, ROW_NUMBER() OVER (
                         PARTITION BY factor_id, other_factor_id, correlation_type
                         ORDER BY abs_correlation DESC, id
                     ) AS rn
                     FROM factor_correlations
                     WHERE other_factor_id IS NOT NULL
                 ) WHERE rn > 1
             )
            """
        ).rowcount
        if resolved or deduped:
            self.log.info(
                "correlation neighbor reconciliation: resolved=%d deduped=%d",
                resolved, deduped,
            )
        return {"resolved": int(resolved or 0), "deduped": int(deduped or 0)}

    def list_unresolved_corr_factors(
        self, *, limit: int | None = None, alpha_id: str | None = None
    ) -> list[dict[str, Any]]:
        """SIMULATED factors with brain ids whose corr verdict is still UNKNOWN.

        Ordered by fewest refresh attempts first so persistently-failing ids
        never starve newer ones.
        """
        sql = [
            "SELECT * FROM factors",
            "WHERE brain_alpha_id IS NOT NULL",
            "  AND status = ?",
            "  AND COALESCE(corr_status, ?) = ?",
        ]
        params: list[Any] = [
            FactorStatus.SIMULATED,
            CorrelationStatus.UNKNOWN.value,
            CorrelationStatus.UNKNOWN.value,
        ]
        if alpha_id:
            sql.append("  AND brain_alpha_id = ?")
            params.append(alpha_id)
        sql.append("ORDER BY corr_check_attempts ASC, id ASC")
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        return [dict(row) for row in self._query("\n".join(sql), tuple(params))]

    def list_corr_backfill_targets(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Factors with a brain id but no final per-neighbour (SELF) evidence.

        A factor leaves the backfill set in either of two ways:

        * it has concrete SELF edges (``factor_correlations`` rows), or
        * a read-only ``GET /alphas/{id}/check`` returned *final*
          SELF_CORRELATION evidence (``corr_evidence_final_at`` set) — a
          zero-neighbour PASS is complete evidence that the graph is correctly
          edgeless, so it must not be re-fetched forever.

        Factors whose check errored, was rate-limited or still reported
        SELF_CORRELATION as PENDING remain targets for the next idempotent run.
        """
        rows = self._query(
            """
            SELECT * FROM factors
             WHERE brain_alpha_id IS NOT NULL
               AND corr_evidence_final_at IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM factor_correlations c
                    WHERE c.factor_id = factors.id
                      AND c.correlation_type = ?
               )
             ORDER BY corr_check_attempts ASC, id DESC
             LIMIT ?
            """,
            (CORR_TYPE_SELF, int(limit)),
        )
        return [dict(row) for row in rows]

    def mark_corr_checked(
        self, factor_id: int, *, error: str | None = None
    ) -> None:
        """Record one correlation refresh attempt (success or failure)."""
        if error:
            self._execute(
                """
                UPDATE factors SET corr_check_attempts = corr_check_attempts + 1,
                                   corr_last_checked_at = ?, corr_error = ?
                 WHERE id = ?
                """,
                (utcnow_iso(), str(error)[:500], factor_id),
            )
        else:
            self._execute(
                """
                UPDATE factors SET corr_check_attempts = corr_check_attempts + 1,
                                   corr_last_checked_at = ?, corr_error = NULL
                 WHERE id = ?
                """,
                (utcnow_iso(), factor_id),
            )

    def mark_corr_evidence_final(
        self, factor_id: int, note: str | None = None
    ) -> None:
        """Record that a /check returned final SELF_CORRELATION evidence.

        Covers both FAIL-with-neighbours (SELF rows saved separately) and the
        zero-neighbour PASS: the latter is authoritative "no edges" and must
        leave the backfill target set even though no correlation row exists.

        ``note`` records *why* evidence is final (see
        :data:`worldquant.registry.migrations.FACTOR_COLUMNS_V5`). It is also
        used for terminal platform-unavailable states (``GATED:*`` /
        ``ALREADY_SUBMITTED``) where the corr verdict stays UNKNOWN. An
        existing note is never overwritten with NULL, so reobserving real
        evidence later upgrades rather than erases the reason.
        """
        now = utcnow_iso()
        if note:
            self._execute(
                """
                UPDATE factors SET corr_check_attempts = corr_check_attempts + 1,
                                   corr_last_checked_at = ?,
                                   corr_evidence_final_at = ?,
                                   corr_evidence_note = ?,
                                   corr_error = NULL
                 WHERE id = ?
                """,
                (now, now, str(note)[:200], factor_id),
            )
        else:
            self._execute(
                """
                UPDATE factors SET corr_check_attempts = corr_check_attempts + 1,
                                   corr_last_checked_at = ?,
                                   corr_evidence_final_at = ?,
                                   corr_error = NULL
                 WHERE id = ?
                """,
                (now, now, factor_id),
            )

    def apply_correlation_gate(self, factor_id: int) -> CorrelationDecision:
        """Compare against the protected set and record PASSED/CORR_REJECTED."""
        gate = CorrelationGate(self.config.correlation)
        rows = self._query(
            """
            SELECT fc.*, f.status AS other_status
            FROM factor_correlations fc
            LEFT JOIN factors f ON f.id = fc.other_factor_id
            WHERE fc.factor_id = ?
            """,
            (factor_id,),
        )
        records = [dict(row) for row in rows]
        include_passed = True  # PASSED alphas are submission candidates too
        protected = [
            row for row in records
            if row.get("other_status") == FactorStatus.SUBMITTED
            or (include_passed and row.get("other_status") == FactorStatus.PASSED)
            # SELF-correlation from BRAIN is always measured against the account's
            # live alphas, so an unresolved neighbor id is still protected.
            or row.get("other_status") is None
        ]
        decision = gate.evaluate(factor_id, protected)
        factor = self.get_factor(factor_id)
        if factor and factor["status"] in (FactorStatus.SIMULATED, FactorStatus.PASSED):
            previous_passed = factor["status"] == FactorStatus.PASSED
            if decision.status is CorrelationStatus.PASS:
                self.set_status(factor_id, FactorStatus.PASSED)
                if not previous_passed:
                    self._bump_combo_counters(factor_id, corr_pass_delta=1)
                if decision.max_abs_corr is not None:
                    self._update_combo_corr(factor_id, decision.max_abs_corr)
                self._execute(
                    """
                    UPDATE factors SET corr_status = ?, corr_band = ?,
                        corr_margin = ?, failure_category = NULL
                    WHERE id = ?
                    """,
                    (
                        CorrelationStatus.PASS.value,
                        decision.band.value,
                        decision.corr_margin,
                        factor_id,
                    ),
                )
            elif decision.status is CorrelationStatus.FAIL:
                self.mark_corr_rejected(factor_id, decision)
            else:
                # UNKNOWN: no usable evidence. Do NOT promote to PASSED and do
                # NOT reject — the factor stays SIMULATED awaiting corr data.
                self._execute(
                    """
                    UPDATE factors SET corr_status = ?, corr_band = NULL,
                        corr_margin = NULL
                    WHERE id = ?
                    """,
                    (CorrelationStatus.UNKNOWN.value, factor_id),
                )
        return decision

    def get_nearest_correlated_factors(
        self, factor_id: int, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT fc.correlation, fc.abs_correlation, fc.correlation_type,
                   fc.other_factor_id, fc.other_brain_alpha_id,
                   f.expression AS other_expression, f.status AS other_status
            FROM factor_correlations fc
            LEFT JOIN factors f ON f.id = fc.other_factor_id
            WHERE fc.factor_id = ?
            ORDER BY fc.abs_correlation DESC
            LIMIT ?
            """,
            (factor_id, limit),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Combination statistics
    # ------------------------------------------------------------------ #
    def _factor_combos(self, factor_id: int) -> list[str]:
        rows = self._query(
            "SELECT combination_keys_json FROM factor_features WHERE factor_id = ?",
            (factor_id,),
        )
        if not rows:
            return []
        return _loads(rows[0]["combination_keys_json"], [])

    def _bump_combo_counters(
        self,
        factor_id: int,
        *,
        trial_delta: int = 0,
        metric_pass_delta: int = 0,
        corr_pass_delta: int = 0,
        submission_delta: int = 0,
    ) -> None:
        keys = set(self._factor_combos(factor_id))
        if not keys:
            return
        now = utcnow_iso()
        placeholders = ",".join("?" for _ in keys)
        rows = self._query(
            f"SELECT combination_key FROM factor_combinations "
            f"WHERE combination_key IN ({placeholders})",
            tuple(keys),
        )
        existing = {row["combination_key"] for row in rows}

        for key in keys:
            if key in existing:
                self._execute(
                    """
                    UPDATE factor_combinations SET
                        trial_count = trial_count + ?,
                        simulation_pass_count = simulation_pass_count + ?,
                        corr_pass_count = corr_pass_count + ?,
                        submission_count = submission_count + ?,
                        updated_at = ?
                    WHERE combination_key = ?
                    """,
                    (trial_delta, metric_pass_delta, corr_pass_delta,
                     submission_delta, now, key),
                )
            else:
                combo = self._combo_meta(key)
                self._execute(
                    """
                    INSERT INTO factor_combinations (
                        combination_key, family_a, family_b, combine_operator,
                        trial_count, simulation_pass_count, corr_pass_count,
                        submission_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key, combo["family_a"], combo["family_b"], combo["operator"],
                        trial_delta, metric_pass_delta, corr_pass_delta,
                        submission_delta, now,
                    ),
                )

    @staticmethod
    def _combo_meta(key: str) -> dict[str, str]:
        parts = key.split("|")
        if len(parts) == 3:
            return {"family_a": parts[0], "operator": parts[1], "family_b": parts[2]}
        return {"family_a": "", "operator": "", "family_b": ""}

    def _update_combo_best(self, factor_id: int) -> None:
        pass  # kept simple: best updated together with trial stats below

    def _record_combo_performance(self, factor_id: int) -> None:
        """Fold one factor's metrics into its combinations (avg/best sharpe/fitness)."""
        metric_rows = self._query(
            "SELECT * FROM factor_metrics WHERE factor_id = ?", (factor_id,)
        )
        if not metric_rows:
            return
        metrics = metric_rows[0]
        keys = set(self._factor_combos(factor_id))
        for key in keys:
            row = self._query(
                "SELECT * FROM factor_combinations WHERE combination_key = ?", (key,)
            )
            if not row:
                continue
            current = dict(row[0])
            sharpe = metrics["sharpe"]
            fitness = metrics["fitness"]
            assignments = ["updated_at = ?"]
            params: list[Any] = [utcnow_iso()]
            if sharpe is not None:
                assignments.append("sharpe_sum = COALESCE(sharpe_sum, 0) + ?")
                params.append(sharpe)
                assignments.append("sharpe_n = sharpe_n + 1")
                if current["best_sharpe"] is None or sharpe > current["best_sharpe"]:
                    assignments.append("best_sharpe = ?")
                    params.append(sharpe)
                    assignments.append("best_factor_id = ?")
                    params.append(factor_id)
            if fitness is not None:
                assignments.append("fitness_sum = COALESCE(fitness_sum, 0) + ?")
                params.append(fitness)
                assignments.append("fitness_n = fitness_n + 1")
                if current["best_fitness"] is None or fitness > current["best_fitness"]:
                    assignments.append("best_fitness = ?")
                    params.append(fitness)
            params.append(key)
            self._execute(
                f"UPDATE factor_combinations SET {', '.join(assignments)} "
                "WHERE combination_key = ?",
                params,
            )

    def _update_combo_corr(self, factor_id: int, abs_corr: float) -> None:
        keys = set(self._factor_combos(factor_id))
        for key in keys:
            self._execute(
                """
                UPDATE factor_combinations SET
                    abs_corr_sum = COALESCE(abs_corr_sum, 0) + ?,
                    abs_corr_n = abs_corr_n + 1, updated_at = ?
                WHERE combination_key = ?
                """,
                (abs_corr, utcnow_iso(), key),
            )

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def find_exact(self, expression: str, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
        exact = dedup_key(expression, normalize_settings(settings))
        rows = self._query("SELECT * FROM factors WHERE exact_hash = ?", (exact,))
        return dict(rows[0]) if rows else None

    def get_factor_by_brain_alpha_id(self, alpha_id: str | None) -> dict[str, Any] | None:
        """Full factor row for a BRAIN alpha id, or None (read-only lookup)."""
        if not alpha_id:
            return None
        rows = self._query(
            "SELECT * FROM factors WHERE brain_alpha_id = ?", (str(alpha_id),)
        )
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------------ #
    # v2 identity layer: signal / experiment / family-template
    # ------------------------------------------------------------------ #
    def find_signal(
        self, signal_hash: str, *, statuses: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Every experiment (settings variant) ever recorded for one signal."""
        sql = "SELECT * FROM factors WHERE signal_hash = ?"
        params: list[Any] = [signal_hash]
        status_list = list(statuses) if statuses else None
        if status_list:
            sql += f" AND status IN ({','.join('?' for _ in status_list)})"
            params.extend(status_list)
        sql += " ORDER BY id"
        return [dict(row) for row in self._query(sql, params)]

    def signal_experiments(
        self,
        expression: str,
        settings: dict[str, Any] | None = None,
        *,
        statuses: Iterable[str] | None = FactorStatus.KNOWLEDGE_SET,
    ) -> list[dict[str, Any]]:
        """Prior researched experiments sharing this signal (expr identity)."""
        fp = self.fingerprints(expression, settings)
        return self.find_signal(fp["signal_hash"], statuses=statuses)

    def count_signal_experiments(self, signal_hash: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM factors WHERE signal_hash = ? "
            "AND status != ?",
            (signal_hash, FactorStatus.GENERATED),
        )
        return int(rows[0]["n"])

    def family_template_trial_count(self, family_template_hash: str) -> int:
        """How often this family-level template (same FAMILY tokens + scope) ran."""
        rows = self._query(
            "SELECT COUNT(*) AS n FROM factors WHERE family_template_hash = ? "
            "AND status != ?",
            (family_template_hash, FactorStatus.GENERATED),
        )
        return int(rows[0]["n"])

    def subtree_matches(
        self,
        leg_template_hashes: Iterable[str] | None = None,
        leg_structure_hashes: Iterable[str] | None = None,
        *,
        statuses: Iterable[str] | None = FactorStatus.RESEARCH_SET,
    ) -> list[dict[str, Any]]:
        """Factors sharing any per-leg fingerprint (subtree-level dedup)."""
        status_list = list(statuses) if statuses else []
        clauses: list[str] = []
        params: list[Any] = []
        templates = [h for h in (leg_template_hashes or []) if h]
        structures = [h for h in (leg_structure_hashes or []) if h]
        if templates:
            clauses.append(
                f"leg_template_hash IN ({','.join('?' for _ in templates)})"
            )
            params.extend(templates)
        if structures:
            clauses.append(
                f"leg_structure_hash IN ({','.join('?' for _ in structures)})"
            )
            params.extend(structures)
        if not clauses:
            return []
        sql = (
            "SELECT DISTINCT s.factor_id, s.node_label, s.leg_index, s.leg_family, "
            "f.expression, f.status FROM factor_subtrees s "
            "JOIN factors f ON f.id = s.factor_id WHERE ("
            + " OR ".join(clauses) + ")"
        )
        if status_list:
            sql += f" AND f.status IN ({','.join('?' for _ in status_list)})"
            params.extend(status_list)
        return [dict(row) for row in self._query(sql, params)]

    def find_subtree_neighbors(self, fp: dict[str, Any]) -> list[dict[str, Any]]:
        """Factors matching any leg of the candidate's blend/group subtrees."""
        template_hashes: set[str] = set()
        structure_hashes: set[str] = set()
        for subtree in fp["features"].subtrees or []:
            for leg in subtree.get("legs", []):
                template_hashes.add(_sha256(leg.get("template", "")))
                structure_hashes.add(_sha256(leg.get("structure", "")))
        return self.subtree_matches(
            leg_template_hashes=template_hashes,
            leg_structure_hashes=structure_hashes,
        )

    def get_factor(self, factor_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM factors WHERE id = ?", (factor_id,))
        return dict(rows[0]) if rows else None

    def get_metrics(self, factor_id: int) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM factor_metrics WHERE factor_id = ?", (factor_id,)
        )
        return dict(rows[0]) if rows else None

    def get_features(self, factor_id: int) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM factor_features WHERE factor_id = ?", (factor_id,)
        )
        if not rows:
            return None
        data = dict(rows[0])
        for key in ("operators_json", "fields_json", "windows_json",
                    "assigned_locals_json", "combination_keys_json", "feature_json"):
            data[key.replace("_json", "")] = _loads(data.pop(key), [])
        return data

    # ------------------------------------------------------------------ #
    # Field metadata cache (read-side enrichment only)
    # ------------------------------------------------------------------ #
    def distinct_used_fields(self) -> list[str]:
        """Every data field referenced by at least one stored factor.

        Reads ``factor_features.fields_json`` (a JSON list), so it reflects
        the fields actually used by historical alphas — no full BRAIN field
        library is ever fetched.
        """
        rows = self._query("SELECT fields_json FROM factor_features")
        seen: set[str] = set()
        for row in rows:
            fields = _loads(row["fields_json"], [])
            if isinstance(fields, list):
                for f in fields:
                    if isinstance(f, str) and f:
                        seen.add(f)
        return sorted(seen)

    def upsert_field_metadata(
        self, field_id: str, payload: dict[str, Any]
    ) -> None:
        """Persist one ``GET /data-fields/{id}`` response.

        Extracts the fields that exist in the real response shape
        (``dataset``, ``category``, ``subcategory``, ``description``, ``type``,
        ``visualizable``) defensively — missing keys stay NULL — and always
        stores the full ``raw_json`` for later introspection.
        """
        dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
        category = payload.get("category") if isinstance(payload.get("category"), dict) else {}
        subcategory = (
            payload.get("subcategory")
            if isinstance(payload.get("subcategory"), dict) else {}
        )
        self._execute(
            """
            INSERT INTO field_metadata (
                field_id, dataset_id, dataset_name, category_id, category_name,
                subcategory_id, subcategory_name, description, field_type,
                visualizable, raw_json, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(field_id) DO UPDATE SET
                dataset_id=excluded.dataset_id,
                dataset_name=excluded.dataset_name,
                category_id=excluded.category_id,
                category_name=excluded.category_name,
                subcategory_id=excluded.subcategory_id,
                subcategory_name=excluded.subcategory_name,
                description=excluded.description,
                field_type=excluded.field_type,
                visualizable=excluded.visualizable,
                raw_json=excluded.raw_json,
                fetched_at=excluded.fetched_at
            """,
            (
                str(field_id),
                dataset.get("id"), dataset.get("name"),
                category.get("id"), category.get("name"),
                subcategory.get("id"), subcategory.get("name"),
                payload.get("description"),
                payload.get("type"),
                1 if payload.get("visualizable") else 0,
                json.dumps(payload, ensure_ascii=False),
                utcnow_iso(),
            ),
        )

    def get_field_metadata(self, field_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM field_metadata WHERE field_id = ?", (str(field_id),)
        )
        if not rows:
            return None
        return dict(rows[0])

    def list_field_metadata(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM field_metadata ORDER BY field_id")]

    def _row_similarity_inputs(
        self, row: sqlite3.Row | dict[str, Any]
    ) -> dict[str, Any]:
        """Parse every feature column a similarity comparison needs."""
        data = dict(row)
        feature_json = _loads(data.get("feature_json"), {}) or {}
        data["operators"] = set(_loads(data.get("operators_json"), []))
        data["fields_list"] = _loads(data.get("fields_json"), [])
        data["windows_list"] = _loads(data.get("windows_json"), [])
        data["combo_keys"] = _loads(data.get("combination_keys_json"), [])
        data["paths"] = _loads(
            data.get("operator_paths_json"),
            feature_json.get("operator_paths", []),
        )
        multiset = _loads(
            data.get("operator_multiset_json"),
            feature_json.get("operator_multiset", {}),
        )
        data["multiset"] = dict(multiset or {})
        families = _loads(
            data.get("field_families_json"),
            feature_json.get("field_families"),
        )
        data["families"] = list(families or [])
        return data

    def _factors_with_features(
        self, *, statuses: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT f.*, ff.operators_json, ff.fields_json, ff.windows_json, "
            "ff.root_operator AS feat_root, ff.tree_depth AS feat_depth, "
            "ff.factor_family AS feat_family, ff.combination_keys_json, "
            "ff.operator_paths_json, ff.operator_multiset_json, "
            "ff.field_families_json, ff.feature_json "
            "FROM factors f LEFT JOIN factor_features ff ON ff.factor_id = f.id"
        )
        params: list[Any] = []
        status_list = list(statuses) if statuses else None
        if status_list:
            sql += f" WHERE f.status IN ({','.join('?' for _ in status_list)})"
            params.extend(status_list)
        return [self._row_similarity_inputs(row) for row in self._query(sql, params)]

    def _similarity_against(
        self, fp: dict[str, Any], row: dict[str, Any]
    ) -> float:
        f = fp["features"]
        return overall_similarity(
            structure_equal=row["structure_hash"] == fp["structure_hash"],
            operators_a=set(f.operators),
            operators_b=row["operators"],
            root_a=f.root_operator,
            root_b=row.get("feat_root") or "",
            depth_a=f.tree_depth,
            depth_b=row.get("feat_depth") or 0,
            fields_a={name.lower() for name in f.fields},
            fields_b={name.lower() for name in row["fields_list"]},
            windows_a=f.windows,
            windows_b=row["windows_list"],
            families_a=fp.get("field_families"),
            families_b=row.get("families"),
            multiset_a=f.operator_multiset,
            multiset_b=row.get("multiset"),
            paths_a=f.operator_paths,
            paths_b=row.get("paths"),
            config=self.config.similarity,
        )

    def find_similar(
        self,
        expression: str,
        settings: dict[str, Any] | None = None,
        *,
        top_k: int = 20,
        statuses: Iterable[str] | None = FactorStatus.RESEARCH_SET,
        use_index: bool = True,
    ) -> list[SimilarFactor]:
        """Nearest researched factors.

        ``use_index=True`` (default) scores only the hash-prefiltered pool and
        scales to tens of thousands of rows; ``use_index=False`` is the
        exhaustive debug mode.
        """
        fp = self.fingerprints(expression, settings)
        if use_index:
            pool = self.research_neighbors(fp, statuses=statuses)
        else:
            pool = self._factors_with_features(statuses=statuses)
        scored: list[SimilarFactor] = []
        for row in pool:
            similarity = self._similarity_against(fp, row)
            scored.append(
                SimilarFactor(
                    factor_id=row["id"], expression=row["expression"],
                    status=row["status"], similarity=similarity,
                    factor_family=row.get("feat_family"),
                    brain_alpha_id=row["brain_alpha_id"],
                )
            )
        scored.sort(key=lambda item: item.similarity, reverse=True)
        return scored[:top_k]

    def research_neighbors(
        self,
        fp: dict[str, Any],
        *,
        statuses: Iterable[str] | None = FactorStatus.RESEARCH_SET,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Index-backed candidate prefilter for similarity/novelty.

        Rather than scanning every expression, the candidate set is the union
        of (a) identical template/structure hashes, (b) identical
        family-template hash, (c) the same factor family and (d) rows sharing
        at least one concrete data field. Fine-grained feature comparison
        happens in Python only on this subset.
        """
        pool_limit = limit or self.config.memory.neighbor_pool_limit
        status_list = list(statuses) if statuses else []
        status_clause = (
            f"AND f.status IN ({','.join('?' for _ in status_list)})"
            if status_list else ""
        )
        arms: list[str] = []
        arm_params: list[Any] = []

        def add_arm(clause: str, params: list[Any]) -> None:
            arms.append(
                "SELECT f.id FROM factors f LEFT JOIN factor_features ff "
                "ON ff.factor_id = f.id WHERE " + clause + f" {status_clause}"
            )
            arm_params.extend(params)
            arm_params.extend(status_list)

        add_arm("f.template_hash = ?", [fp["template_hash"]])
        add_arm("f.structure_hash = ?", [fp["structure_hash"]])
        if fp.get("family_template_hash"):
            add_arm(
                "f.family_template_hash = ?", [fp["family_template_hash"]]
            )
        family = fp.get("factor_family")
        if family and family != "UNKNOWN":
            add_arm("ff.factor_family = ?", [family])
        fields = fp["features"].fields
        if fields:
            like_clause = " OR ".join(
                "lower(ff.fields_json) LIKE ?" for _ in fields
            )
            add_arm(
                f"({like_clause})",
                [f'%"{name.lower()}"%' for name in fields],
            )

        union_sql = " UNION ".join(arms) if arms else "SELECT 0 WHERE 0"
        rows = self._query(
            f"""
            SELECT f.*, ff.operators_json, ff.fields_json, ff.windows_json,
                   ff.root_operator AS feat_root, ff.tree_depth AS feat_depth,
                   ff.factor_family AS feat_family, ff.combination_keys_json,
                   ff.operator_paths_json, ff.operator_multiset_json,
                   ff.field_families_json, ff.feature_json
            FROM factors f
            LEFT JOIN factor_features ff ON ff.factor_id = f.id
            WHERE f.id IN ({union_sql})
            LIMIT ?
            """,
            (*arm_params, pool_limit),
        )
        return [self._row_similarity_inputs(row) for row in rows]

    def get_factors_by_status(self, status: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM factors WHERE status = ? ORDER BY id DESC"
        params: list[Any] = [status]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(row) for row in self._query(sql, params)]

    def get_submitted_factors(self) -> list[dict[str, Any]]:
        return self.get_factors_by_status(FactorStatus.SUBMITTED)

    def get_rejected_factors(self) -> list[dict[str, Any]]:
        return [
            factor
            for status in (
                FactorStatus.METRIC_REJECTED,
                FactorStatus.CORR_REJECTED,
                FactorStatus.DUPLICATE,
                FactorStatus.SIMULATION_FAILED,
            )
            for factor in self.get_factors_by_status(status)
        ]

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    def status_counts(self) -> dict[str, int]:
        return {
            str(row["status"]): int(row["n"])
            for row in self._query(
                "SELECT status, COUNT(*) AS n FROM factors GROUP BY status"
            )
        }

    def stats(self) -> dict[str, Any]:
        counts = self.status_counts()
        total = sum(counts.values())
        features_total = self._query(
            "SELECT COUNT(*) AS n FROM factor_features"
        )[0]["n"]
        return {
            "total": total,
            "generated": counts.get(FactorStatus.GENERATED, 0),
            "simulated": sum(
                counts.get(status, 0)
                for status in (FactorStatus.SIMULATED, FactorStatus.PASSED,
                               FactorStatus.SUBMITTED, FactorStatus.METRIC_REJECTED,
                               FactorStatus.CORR_REJECTED)
            ),
            "submitted": counts.get(FactorStatus.SUBMITTED, 0),
            "duplicates_blocked": counts.get(FactorStatus.DUPLICATE, 0),
            "correlation_rejected": counts.get(FactorStatus.CORR_REJECTED, 0),
            "metric_rejected": counts.get(FactorStatus.METRIC_REJECTED, 0),
            "simulation_failed": counts.get(FactorStatus.SIMULATION_FAILED, 0),
            "passed": counts.get(FactorStatus.PASSED, 0),
            "features_indexed": int(features_total),
            "status_counts": counts,
        }

    def get_family_stats(self) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT ff.factor_family AS family,
                   COUNT(*) AS trials,
                   SUM(CASE WHEN f.status = 'SUBMITTED' THEN 1 ELSE 0 END) AS submitted,
                   SUM(CASE WHEN f.status IN ('PASSED','SUBMITTED') THEN 1 ELSE 0 END) AS passed,
                   SUM(CASE WHEN f.status = 'CORR_REJECTED' THEN 1 ELSE 0 END) AS corr_rejected,
                   AVG(m.sharpe) AS avg_sharpe,
                   MAX(m.fitness) AS best_fitness
            FROM factor_features ff
            JOIN factors f ON f.id = ff.factor_id
            LEFT JOIN factor_metrics m ON m.factor_id = f.id
            GROUP BY ff.factor_family
            ORDER BY trials DESC
            """
        )
        return [dict(row) for row in rows]

    def get_combination_stats(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM factor_combinations ORDER BY trial_count DESC"
        )
        result = []
        for row in rows:
            data = dict(row)
            data["avg_sharpe"] = (
                data["sharpe_sum"] / data["sharpe_n"] if data["sharpe_n"] else None
            )
            data["avg_fitness"] = (
                data["fitness_sum"] / data["fitness_n"] if data["fitness_n"] else None
            )
            data["avg_abs_corr"] = (
                data["abs_corr_sum"] / data["abs_corr_n"] if data["abs_corr_n"] else None
            )
            result.append(data)
        return result

    def get_saturated_combinations(
        self, *, min_trials: int | None = None, max_submit_rate: float = 0.1
    ) -> list[dict[str, Any]]:
        stats = self.get_combination_stats()
        return [
            row for row in stats
            if row["trial_count"] >= (min_trials or 10)
            and (row["submission_count"] / row["trial_count"]) <= max_submit_rate
        ]

    def get_underexplored_combinations(self, *, max_trials: int = 3) -> list[dict[str, Any]]:
        return [
            row for row in self.get_combination_stats()
            if row["trial_count"] <= max_trials
        ]

    def get_underexplored_families(self, *, max_trials: int = 5) -> list[dict[str, Any]]:
        return [
            row for row in self.get_family_stats() if row["trials"] <= max_trials
        ]

    def set_novelty_score(self, factor_id: int, score: float) -> None:
        self._execute(
            "UPDATE factors SET novelty_score = ? WHERE id = ?", (score, factor_id)
        )

    def template_trial_count(self, template_hash: str) -> int:
        """How often this template was registered (any status except raw GENERATED)."""
        rows = self._query(
            "SELECT COUNT(*) AS n FROM factors WHERE template_hash = ? "
            "AND status != ?",
            (template_hash, FactorStatus.GENERATED),
        )
        return int(rows[0]["n"])

    def count_field_usage(self, field_name: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM factor_features "
            "WHERE lower(fields_json) LIKE ?",
            (f'%"{field_name.lower()}"%',),
        )
        return int(rows[0]["n"])

    # ------------------------------------------------------------------ #
    # Public candidate evaluation (read-only; generators never touch SQL)
    # ------------------------------------------------------------------ #
    def evaluate_candidate(
        self,
        expression: str,
        settings: dict[str, Any] | None = None,
        *,
        force: bool = False,
        source: str | None = None,
        ablation_group_id: str | None = None,
        changed_parameters: dict[str, Any] | None = None,
        parent_experiment_id: int | None = None,
    ) -> dict[str, Any]:
        """Four-level duplicate + novelty verdict as plain JSON-serializable data.

        Levels:
            L0 exact experiment (expr + settings)      -> hard reject
            L1 same signal, different settings         -> penalty / skip
            L2 family/template saturation              -> novelty penalty
            L3 structural neighbours (incl. subtrees)   -> novelty score
        """
        from .gates import (
            ACTION_REJECT_EXACT,
            ACTION_SIMULATE,
            ACTION_SKIP_LOW_NOVELTY,
            ACTION_SKIP_SIGNAL_DUPLICATE,
            calculate_novelty,
        )

        source = source or "generator"
        is_ablation = source == "ablation" or bool(ablation_group_id)
        fp = self.fingerprints(expression, settings)
        cfg = self.config
        warnings: list[str] = []

        # --- L0: exact experiment -----------------------------------------
        exact_row = self.find_exact(expression, settings)
        experiment_duplicate = exact_row is not None

        # --- L1: same signal, different settings --------------------------
        prior = self.find_signal(
            fp["signal_hash"], statuses=FactorStatus.KNOWLEDGE_SET
        )
        prior = [row for row in prior if not experiment_duplicate or row["id"] != (exact_row or {}).get("id")]
        signal_trials = self.count_signal_experiments(fp["signal_hash"])
        same_signal = signal_trials > 0
        if same_signal:
            ids = [row["id"] for row in prior] or (
                [int(exact_row["id"])] if exact_row else []
            )
            warnings.append(
                f"same canonical expression already has {signal_trials} experiment(s): "
                f"factor ids {ids[:8]}"
            )

        # --- L2: template / family-template saturation --------------------
        template_trials = self.template_trial_count(fp["template_hash"])
        family_template_trials = self.family_template_trial_count(
            fp["family_template_hash"]
        )
        subtree_neighbors = self.find_subtree_neighbors(fp)
        if subtree_neighbors:
            warnings.append(
                f"{len(subtree_neighbors)} prior factor(s) share a blend/group leg"
            )

        # --- L3: structural novelty (with same-signal penalty) -------------
        novelty = calculate_novelty(
            self,
            expression,
            settings,
            same_signal=same_signal and not is_ablation,
            signal_trials=signal_trials,
        )
        nearest_factor = None
        nearest_cluster = None
        if novelty.nearest_factor_id is not None:
            nearest_factor = {
                "factor_id": novelty.nearest_factor_id,
                "similarity": novelty.nearest_similarity,
            }
            nf = self.get_factor(novelty.nearest_factor_id)
            if nf:
                nearest_factor["expression"] = nf.get("expression")
                nearest_factor["status"] = nf.get("status")
            nearest_cluster = self._cluster_of(novelty.nearest_factor_id)

        # --- Decision ------------------------------------------------------
        action = ACTION_SIMULATE
        reasons: list[str] = []
        if experiment_duplicate and cfg.pre_simulation.reject_exact_duplicate and not force:
            action = ACTION_REJECT_EXACT
            reasons.append(
                f"exact experiment already recorded as factor#{exact_row['id']}"
                if exact_row else "exact experiment already recorded"
            )
        elif (
            not force
            and not is_ablation
            and same_signal
            and signal_trials >= cfg.pre_simulation.max_signal_experiments
        ):
            action = ACTION_SKIP_SIGNAL_DUPLICATE
            reasons.append(
                f"same signal already researched {signal_trials} times "
                f"(>= {cfg.pre_simulation.max_signal_experiments}); change data/legs, "
                "not truncation/decay/window (use source='ablation' for a deliberate sweep)"
            )
        elif (
            cfg.pre_simulation.reject_if_saturated_and_low_novelty
            and template_trials >= cfg.pre_simulation.max_template_trials
            and novelty.score < cfg.pre_simulation.min_novelty
        ):
            action = ACTION_SKIP_LOW_NOVELTY
            reasons.append(
                f"template tried {template_trials} times and novelty "
                f"{novelty.score:.1f} < {cfg.pre_simulation.min_novelty:.1f}"
            )

        if is_ablation and same_signal:
            warnings.append(
                "ablation run: same-signal skip suppressed by design"
            )
        if force and (experiment_duplicate or action != ACTION_SIMULATE):
            warnings.append("force=true: duplicate/saturation reject overridden")
        if not reasons:
            reasons.append(
                f"novelty {novelty.score:.1f}; signal_trials={signal_trials}; "
                f"template_trials={template_trials}; "
                f"family_template_trials={family_template_trials}"
            )

        return {
            "action": action,
            "expression": fp["canonical"],
            "level": action,
            "reason": "; ".join(reasons),
            "warnings": warnings,
            "experiment_duplicate": experiment_duplicate,
            "experiment_factor_id": int(exact_row["id"]) if exact_row else None,
            "signal_duplicate": same_signal,
            "same_signal": same_signal,
            "signal_trials": signal_trials,
            "previous_experiments": [row["id"] for row in prior][:20],
            "template_trials": template_trials,
            "family_template_trials": family_template_trials,
            "subtree_neighbor_count": len(subtree_neighbors),
            "novelty": novelty.to_dict(),
            "nearest_factor": nearest_factor,
            "nearest_similarity": novelty.nearest_similarity,
            "nearest_cluster": nearest_cluster,
            "ablation": {
                "is_ablation": is_ablation,
                "ablation_group_id": ablation_group_id,
                "changed_parameters": changed_parameters,
                "parent_experiment_id": parent_experiment_id,
            },
            "hashes": {
                "signal_hash": fp["signal_hash"],
                "experiment_hash": fp["experiment_hash"],
                "exact_hash": fp["exact_hash"],
                "template_hash": fp["template_hash"],
                "family_template_hash": fp["family_template_hash"],
                "structure_hash": fp["structure_hash"],
            },
        }

    def _cluster_of(self, factor_id: int) -> dict[str, Any] | None:
        """Small cluster descriptor for one factor (nearest-factor context)."""
        try:
            from .clusters import get_clusters

            for cluster in get_clusters(self, min_size=2):
                if factor_id in cluster.factor_ids:
                    return {
                        "cluster_id": cluster.cluster_id,
                        "representative_id": cluster.representative_id,
                        "size": cluster.size,
                        "submitted_count": cluster.submitted_count,
                    }
        except Exception as exc:  # noqa: BLE001 - enrichment must never block
            self.log.debug("cluster lookup failed: %s", exc)
        return None

    # ------------------------------------------------------------------ #
    # v2 backfill (parser-dependent part of the migration)
    # ------------------------------------------------------------------ #
    def _backfill_v2(self) -> None:
        """Populate v2 columns for pre-v2 rows without touching their history."""
        rows = self._query(
            "SELECT id FROM factors ORDER BY id"
        )
        factor_ids = [int(row["id"]) for row in rows]
        if not factor_ids:
            return
        self.log.info("backfilling v2 fingerprints for %d factors", len(factor_ids))
        for factor_id in factor_ids:
            try:
                self._backfill_one_factor(factor_id)
            except Exception as exc:  # noqa: BLE001 - one bad row must not abort
                self.log.warning("v2 backfill skipped factor#%d: %s", factor_id, exc)
        self._backfill_combo_legs(factor_ids)
        self._backfill_corr_states(factor_ids)
        self._backfill_cluster_links()

    def _backfill_one_factor(self, factor_id: int) -> None:
        factor = self.get_factor(factor_id)
        if not factor:
            return
        settings = _loads(factor.get("settings_json"), {})
        fp = self.fingerprints(factor["expression"], settings)

        theme = classify_theme(
            fp["features"].fields,
            fp["features"].operators,
            resolver=self.resolver,
        )
        parts = (theme or "").split(".")
        self._execute(
            """
            UPDATE factors SET
                signal_hash = COALESCE(signal_hash, ?),
                experiment_hash = COALESCE(experiment_hash, exact_hash),
                family_template_hash = COALESCE(family_template_hash, ?),
                theme = COALESCE(theme, ?),
                branch = COALESCE(branch, ?),
                subtheme = COALESCE(subtheme, ?)
            WHERE id = ?
            """,
            (
                fp["signal_hash"], fp["family_template_hash"],
                parts[0] if parts else None,
                parts[1] if len(parts) > 1 else None,
                parts[2] if len(parts) > 2 else None,
                factor_id,
            ),
        )
        self._save_features(factor_id, fp)
        self._save_subtrees(factor_id, fp)

        if factor.get("failure_category"):
            return
        category: str | None = None
        status = factor["status"]
        if status == FactorStatus.CORR_REJECTED:
            category = _FAILURE_SELF_CORRELATION
        elif status == FactorStatus.SIMULATION_FAILED:
            category = _FAILURE_SIM_ERROR
        elif status == FactorStatus.DUPLICATE:
            category = _FAILURE_DUPLICATE
        elif status == FactorStatus.METRIC_REJECTED:
            metrics = self.get_metrics(factor_id) or {}
            assessment = classify_failure(
                status=status,
                reasons=[factor.get("rejection_reason") or ""],
                sharpe=metrics.get("sharpe"),
                fitness=metrics.get("fitness"),
                long_count=metrics.get("long_count"),
                short_count=metrics.get("short_count"),
                train_sharpe=metrics.get("sharpe"),
                test_sharpe=metrics.get("test_sharpe"),
            )
            category = assessment.primary
        if category:
            self._execute(
                "UPDATE factors SET failure_category = ? WHERE id = ?",
                (category, factor_id),
            )

        # high_quality_redundant for historical corr rejections.
        if status == FactorStatus.CORR_REJECTED and self._is_high_quality(factor_id):
            self._execute(
                "UPDATE factors SET high_quality_redundant = 1 WHERE id = ?",
                (factor_id,),
            )

    def _backfill_v3(self) -> None:
        """Fill the v3 identity columns for every factor.

        ``expression_hash`` is COALESCE-filled; ``signal_hash`` is recomputed
        unconditionally because the v2 definition was expression-only while v3
        scopes it by region/universe/delay. Both hashes come straight from
        :mod:`worldquant.hashing` (no expression parser needed), so this is
        cheap enough to rerun wholesale and safe to interrupt/resume.
        """
        rows = self._query(
            "SELECT id, canonical_expression, settings_json FROM factors"
        )
        updated = 0
        for row in rows:
            canonical = row["canonical_expression"] or ""
            settings = _loads(row["settings_json"], {})
            self._execute(
                """
                UPDATE factors
                   SET expression_hash = COALESCE(expression_hash, ?),
                       signal_hash = ?
                 WHERE id = ?
                """,
                (expression_hash(canonical), signal_identity(canonical, settings),
                 row["id"]),
            )
            updated += 1
        self.log.info("registry v3 identity backfill applied to %d factors", updated)

    def _backfill_combo_legs(self, factor_ids: list[int]) -> None:
        """Attach per-leg hashes to combination rows (old + new factors)."""
        for factor_id in factor_ids:
            try:
                factor = self.get_factor(factor_id)
                if not factor:
                    continue
                settings = _loads(factor.get("settings_json"), {})
                fp = self.fingerprints(factor["expression"], settings)
                for combo in fp["combinations"]:
                    left = getattr(combo, "left_leg", None)
                    right = getattr(combo, "right_leg", None)
                    structure_hash = getattr(combo, "structure_hash", None)
                    if left is None and right is None:
                        continue
                    self._execute(
                        """
                        UPDATE factor_combinations SET
                            left_template_hash = COALESCE(left_template_hash, ?),
                            right_template_hash = COALESCE(right_template_hash, ?),
                            left_structure_hash = COALESCE(left_structure_hash, ?),
                            right_structure_hash = COALESCE(right_structure_hash, ?),
                            structure_hash = COALESCE(structure_hash, ?)
                        WHERE combination_key = ?
                        """,
                        (
                            left.get("template_hash") if left else None,
                            right.get("template_hash") if right else None,
                            left.get("structure_hash") if left else None,
                            right.get("structure_hash") if right else None,
                            structure_hash,
                            combo.key,
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                self.log.debug("combo leg backfill failed for #%d: %s", factor_id, exc)

    def _backfill_corr_states(self, factor_ids: list[int]) -> None:
        """Stamp corr_status/band/margin from stored correlations (history-safe)."""
        gate = CorrelationGate(self.config.correlation)
        for factor_id in factor_ids:
            factor = self.get_factor(factor_id)
            if not factor or factor.get("corr_status"):
                continue
            rows = self._query(
                """
                SELECT fc.*, f.status AS other_status
                FROM factor_correlations fc
                LEFT JOIN factors f ON f.id = fc.other_factor_id
                WHERE fc.factor_id = ?
                """,
                (factor_id,),
            )
            if not rows:
                continue
            protected = [
                dict(row) for row in rows
                if dict(row).get("other_status") in (
                    FactorStatus.SUBMITTED, FactorStatus.PASSED, None
                )
            ]
            if not protected:
                continue
            decision = gate.evaluate(factor_id, protected)
            self._execute(
                "UPDATE factors SET corr_status = ?, corr_band = ?, corr_margin = ? "
                "WHERE id = ?",
                (
                    decision.status.value,
                    decision.band.value if decision.band else None,
                    decision.corr_margin,
                    factor_id,
                ),
            )

    def _backfill_cluster_links(self) -> None:
        try:
            from .clusters import get_clusters

            for cluster in get_clusters(self, min_size=2):
                placeholders = ",".join("?" for _ in cluster.factor_ids)
                self._execute(
                    f"UPDATE factors SET nearest_cluster_id = ? "
                    f"WHERE id IN ({placeholders})",
                    (cluster.cluster_id, *cluster.factor_ids),
                )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("cluster link backfill failed: %s", exc)
