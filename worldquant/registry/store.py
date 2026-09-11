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
from ..hashing import _HASH_SEPARATOR, dedup_key, normalize_expression
from ..logging_utils import get_logger
from ..storage import utcnow_iso
from .combinations import Combination, extract_combinations
from .config import RegistryConfig
from .correlation import CorrelationDecision, CorrelationGate
from .expr_parser import analyze_expression, parse_expression_ast
from .families import FieldFamilyResolver, classify_factor_family
from .scoring import quality_score
from .similarity import jaccard, overall_similarity, window_set_similarity

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
    novelty_score        REAL
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
    feature_json         TEXT
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
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_factors_template    ON factors(template_hash);
CREATE INDEX IF NOT EXISTS idx_factors_structure   ON factors(structure_hash);
CREATE INDEX IF NOT EXISTS idx_factors_status      ON factors(status);
CREATE INDEX IF NOT EXISTS idx_factors_brain_alpha ON factors(brain_alpha_id);
CREATE INDEX IF NOT EXISTS idx_features_family     ON factor_features(factor_family);
CREATE INDEX IF NOT EXISTS idx_corr_factor         ON factor_correlations(factor_id);
CREATE INDEX IF NOT EXISTS idx_corr_other          ON factor_correlations(other_factor_id);
CREATE INDEX IF NOT EXISTS idx_corr_brain_other    ON factor_correlations(other_brain_alpha_id);
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
                self._conn.executescript(_SCHEMA)
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

        return {
            "exact_hash": exact,
            "canonical": canonical,
            "template_hash": _sha256(features.template + _HASH_SEPARATOR + scope),
            "structure_hash": _sha256(features.structure),
            "field_template": features.field_template,
            "family_template": features.family_template,
            "features": features,
            "field_family": primary_family,
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
    ) -> RegisteredCandidate:
        """Insert a GENERATED candidate, or return the existing factor. Idempotent."""
        fp = self.fingerprints(expression, settings)
        exact_hash = fp["exact_hash"]

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

        scope = fp["scope_settings"]
        now = utcnow_iso()
        cursor = self._execute(
            """
            INSERT INTO factors (
                exact_hash, expression, canonical_expression,
                template_hash, structure_hash, field_template, family_template,
                status, region, universe, delay, decay, neutralization, truncation,
                settings_json, created_at, brain_alpha_id, brain_simulation_id,
                source, parent_factor_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        factor_id = int(cursor.lastrowid)
        self._save_features(factor_id, fp)
        return RegisteredCandidate(
            factor_id=factor_id, created=True, exact_hash=exact_hash,
            status=FactorStatus.GENERATED,
        )

    def _save_features(self, factor_id: int, fp: dict[str, Any]) -> None:
        f = fp["features"]
        combo_keys = [combo.key for combo in fp["combinations"]]
        feature_json = f.to_feature_json()
        feature_json["field_families"] = [
            self.resolver(name) for name in f.fields
        ]
        self._execute(
            """
            INSERT OR REPLACE INTO factor_features (
                factor_id, operators_json, fields_json, windows_json,
                operator_count, field_count, tree_depth, root_operator,
                factor_family, field_family, assigned_locals_json,
                combination_keys_json, feature_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                factor_id, _dumps(f.operators), _dumps(f.fields), _dumps(f.windows),
                f.operator_count, f.field_count, f.tree_depth, f.root_operator,
                fp["factor_family"], fp["field_family"],
                _dumps(f.assigned_locals), _dumps(combo_keys), _dumps(feature_json),
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
            reasons = getattr(result, "reasons", None) or [
                "did not pass configured metric filters"
            ]
            self.mark_metric_rejected(factor_id, list(reasons))

        if first_completion:
            self._bump_combo_counters(
                factor_id,
                trial_delta=1,
                metric_pass_delta=1 if metric_passed else 0,
            )
            # Fold Sharpe/Fitness into per-combination avg/best exactly once,
            # when the first completed metrics land (not on re-imports).
            self._record_combo_performance(factor_id)
        return factor_id

    def handle_completed(
        self,
        result: Any,
        *,
        corr_records: list[dict[str, Any]] | None = None,
        apply_gate: bool = True,
    ) -> dict[str, Any]:
        """One-call adapter hook: metrics -> metric reject -> correlation gate.

        Returns ``{"factor_id", "status", "correlation_decision"}``. Never
        raises: registry bookkeeping must not break a backtest run.
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
                    self.save_correlations(factor_id, records, CORR_TYPE_SELF)
                elif max_self is not None:
                    self.save_correlations(
                        factor_id,
                        [{"correlation": max_self, "other_brain_alpha_id": None}],
                        CORR_TYPE_SELF_MAX,
                    )
                if apply_gate and self.config.correlation.enabled and (records or max_self is not None):
                    decision = self.apply_correlation_gate(factor_id)
            return {
                "factor_id": factor_id,
                "status": self.get_factor(factor_id)["status"],
                "correlation_decision": decision,
            }
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self.log.warning("registry.handle_completed failed: %s", exc)
            return {"factor_id": None, "status": None, "correlation_decision": None,
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
            if decision.passed:
                self.set_status(factor_id, FactorStatus.PASSED)
                if not previous_passed:
                    self._bump_combo_counters(factor_id, corr_pass_delta=1)
                if decision.max_abs_corr is not None:
                    self._update_combo_corr(factor_id, decision.max_abs_corr)
            else:
                self.mark_corr_rejected(factor_id, decision)
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

    def _factors_with_features(
        self, *, statuses: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT f.*, ff.operators_json, ff.fields_json, ff.windows_json, "
            "ff.root_operator AS feat_root, ff.tree_depth AS feat_depth, "
            "ff.factor_family AS feat_family, ff.combination_keys_json "
            "FROM factors f LEFT JOIN factor_features ff ON ff.factor_id = f.id"
        )
        params: list[Any] = []
        status_list = list(statuses) if statuses else None
        if status_list:
            sql += f" WHERE f.status IN ({','.join('?' for _ in status_list)})"
            params.extend(status_list)
        rows = self._query(sql, params)
        result = []
        for row in rows:
            data = dict(row)
            data["operators"] = set(_loads(row["operators_json"], []))
            data["fields_list"] = _loads(row["fields_json"], [])
            data["windows_list"] = _loads(row["windows_json"], [])
            data["combo_keys"] = _loads(row["combination_keys_json"], [])
            result.append(data)
        return result

    def find_similar(
        self,
        expression: str,
        settings: dict[str, Any] | None = None,
        *,
        top_k: int = 20,
        statuses: Iterable[str] | None = FactorStatus.RESEARCH_SET,
    ) -> list[SimilarFactor]:
        fp = self.fingerprints(expression, settings)
        f = fp["features"]
        scored: list[SimilarFactor] = []
        for row in self._factors_with_features(statuses=statuses):
            similarity = overall_similarity(
                structure_equal=row["structure_hash"] == fp["structure_hash"],
                operators_a=set(f.operators),
                operators_b=row["operators"],
                root_a=f.root_operator,
                root_b=row["feat_root"] or "",
                depth_a=f.tree_depth,
                depth_b=row["feat_depth"] or 0,
                fields_a={name.lower() for name in f.fields},
                fields_b={name.lower() for name in row["fields_list"]},
                windows_a=f.windows,
                windows_b=row["windows_list"],
            )
            scored.append(
                SimilarFactor(
                    factor_id=row["id"], expression=row["expression"],
                    status=row["status"], similarity=similarity,
                    factor_family=row["feat_family"],
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
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Index-backed candidate prefilter for similarity/novelty.

        Rather than scanning every expression, the candidate set is the union
        of (a) identical template/structure hashes, (b) the same factor family
        and (c) rows sharing at least one concrete data field. Fine-grained
        feature comparison happens in Python only on this subset.
        """
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

        union_sql = " UNION ".join(arms)
        rows = self._query(
            f"""
            SELECT f.*, ff.operators_json, ff.fields_json, ff.windows_json,
                   ff.root_operator AS feat_root, ff.tree_depth AS feat_depth,
                   ff.factor_family AS feat_family, ff.combination_keys_json
            FROM factors f
            LEFT JOIN factor_features ff ON ff.factor_id = f.id
            WHERE f.id IN ({union_sql})
            LIMIT ?
            """,
            (*arm_params, limit),
        )
        result = []
        for row in rows:
            data = dict(row)
            data["operators"] = set(_loads(row["operators_json"], []))
            data["fields_list"] = _loads(row["fields_json"], [])
            data["windows_list"] = _loads(row["windows_json"], [])
            data["combo_keys"] = _loads(row["combination_keys_json"], [])
            result.append(data)
        return result

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
