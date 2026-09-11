"""SQLite persistence and CSV export.

SQLite is the source of truth; the CSV files under ``data/`` are derived views
that can be regenerated at any time with ``--export-only``. Raw server payloads
are kept in ``results.raw_json`` so that metrics can be re-parsed later without
re-running a backtest if BRAIN changes its field names.

The raw payload is scrubbed of credential-shaped keys before it is written, so
authentication material can never land in the database.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .api import SimulationStatus, all_checks_passed
from .exceptions import StorageError
from .hashing import dedup_key, expression_hash, scope_hash
from .logging_utils import get_logger
from .models import AlphaResult

_SENSITIVE_KEYS = frozenset(
    {"password", "token", "authorization", "cookie", "cookies", "secret", "session", "inquiry"}
)

CSV_COLUMNS: tuple[str, ...] = (
    "alpha_id", "expression", "dedup_key", "status", "grade", "stage",
    "simulation_id", "remote_alpha_id",
    "sharpe", "fitness", "turnover", "returns", "drawdown", "margin", "pnl", "book_size",
    "long_count", "short_count",
    "positive_years", "negative_years", "total_years", "positive_year_ratio",
    "yearly_sharpe_std", "worst_year_sharpe",
    "passed", "submittable", "checks_passed", "checks_failed", "self_correlation",
    "reasons", "created_at", "completed_at", "error",
    "settings_json", "yearly_stats_json",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alphas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key       TEXT    NOT NULL UNIQUE,
    name            TEXT,
    expression      TEXT    NOT NULL,
    expression_hash TEXT    NOT NULL,
    settings_json   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS simulations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    alpha_id             INTEGER NOT NULL REFERENCES alphas(id) ON DELETE CASCADE,
    remote_simulation_id TEXT,
    remote_alpha_id      TEXT,
    status               TEXT    NOT NULL,
    submitted_at         TEXT,
    completed_at         TEXT,
    error                TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id     INTEGER NOT NULL UNIQUE REFERENCES simulations(id) ON DELETE CASCADE,
    sharpe            REAL,
    fitness           REAL,
    turnover          REAL,
    returns           REAL,
    drawdown          REAL,
    margin            REAL,
    pnl               REAL,
    book_size         REAL,
    long_count        INTEGER,
    short_count       INTEGER,
    grade             TEXT,
    stage             TEXT,
    train_json        TEXT,
    test_json         TEXT,
    submittable       INTEGER,
    self_correlation  REAL,
    yearly_stats_json TEXT,
    checks_json       TEXT,
    raw_json          TEXT,
    passed            INTEGER,
    reasons           TEXT,
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_simulations_alpha   ON simulations(alpha_id);
CREATE INDEX IF NOT EXISTS idx_simulations_status  ON simulations(status);
CREATE INDEX IF NOT EXISTS idx_alphas_expr_hash    ON alphas(expression_hash);
"""

#: Explicit joined projection shared by every read path. Wildcards are avoided
#: because ``simulations`` and ``results`` share column names (``id``,
#: ``created_at``), and a collision would silently pick the wrong value.
_JOINED_SELECT = """
SELECT a.dedup_key, a.name, a.expression, a.settings_json, a.created_at,
       s.id AS simulation_row_id, s.remote_simulation_id, s.remote_alpha_id,
       s.status, s.submitted_at, s.completed_at, s.error,
       r.sharpe, r.fitness, r.turnover, r.returns, r.drawdown, r.margin,
       r.pnl, r.book_size, r.long_count, r.short_count,
       r.grade, r.stage,
       r.train_json, r.test_json,
       r.submittable, r.self_correlation, r.excluded_reason,
       r.yearly_stats_json, r.checks_json, r.raw_json, r.passed, r.reasons
FROM simulations s
JOIN alphas a ON a.id = s.alpha_id
LEFT JOIN results r ON r.simulation_id = s.id
""".strip()


def utcnow_iso() -> str:
    """Timezone-aware ISO-8601 timestamp, stable across Windows and Linux."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_sensitive(payload: Any, depth: int = 0) -> Any:
    """Drop credential-shaped keys from a payload before it is persisted."""
    if depth > 8:
        return "..."
    if isinstance(payload, dict):
        return {
            key: ("<redacted>" if str(key).lower() in _SENSITIVE_KEYS else redact_sensitive(value, depth + 1))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_sensitive(item, depth + 1) for item in payload]
    return payload


class ResultStore:
    """Thread-safe SQLite store for alphas, simulations and results."""

    def __init__(self, db_path: str | Path, *, logger: Any = None) -> None:
        self.db_path = Path(db_path)
        self.log = logger or get_logger("storage")
        self._lock = threading.RLock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path), timeout=30.0, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open database at {self.db_path}: {exc}") from exc

        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                # WAL lets a reader (e.g. --export-only) run while a batch writes.
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.executescript(_SCHEMA)
                self._migrate()
                self._conn.commit()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot initialize schema in {self.db_path}: {exc}") from exc

    #: Additive columns that a database created before they existed may lack.
    #: ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so these
    #: are back-filled by :meth:`_migrate`.
    _RESULTS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
        ("grade", "TEXT"),
        ("stage", "TEXT"),
        ("train_json", "TEXT"),
        ("test_json", "TEXT"),
        ("submittable", "INTEGER"),
        ("self_correlation", "REAL"),
        # Permanent exclusion tag set by mark_excluded() — once an alpha has
        # been screened out for a structural reason (SELF_CORRELATION over the
        # 0.7 limit against an already-submitted family member, a degenerate
        # long-only book), it stays out of every future "usable alpha" list
        # without anyone having to re-derive the verdict from the checks.
        ("excluded_reason", "TEXT"),
    )

    def _migrate(self) -> None:
        """Back-fill columns added after a database was first created."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(results)").fetchall()
        }
        for column, column_type in self._RESULTS_ADDED_COLUMNS:
            if column in existing:
                continue
            self._conn.execute(f"ALTER TABLE results ADD COLUMN {column} {column_type}")
            self.log.info("migrated %s: added results.%s column", self.db_path, column)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "ResultStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor
        except sqlite3.Error as exc:
            raise StorageError(f"database write failed: {exc} | sql={sql.strip()[:120]}") from exc

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            with self._lock:
                return list(self._conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise StorageError(f"database read failed: {exc} | sql={sql.strip()[:120]}") from exc

    # ------------------------------------------------------------------ #
    # Alphas
    # ------------------------------------------------------------------ #
    def upsert_alpha(
        self,
        expression: str,
        settings: dict[str, Any] | None,
        *,
        name: str | None = None,
    ) -> int:
        """Insert an alpha, or return the existing row id for the same key."""
        key = dedup_key(expression, settings)
        settings_json = json.dumps(settings or {}, sort_keys=True)
        existing = self._query("SELECT id FROM alphas WHERE dedup_key = ?", (key,))
        if existing:
            row_id = int(existing[0]["id"])
            if name:
                self._execute("UPDATE alphas SET name = ? WHERE id = ?", (name, row_id))
            return row_id

        cursor = self._execute(
            "INSERT INTO alphas (dedup_key, name, expression, expression_hash, settings_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, name, expression, expression_hash(expression), settings_json, utcnow_iso()),
        )
        return int(cursor.lastrowid)

    def get_alpha(self, alpha_row_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM alphas WHERE id = ?", (alpha_row_id,))
        return dict(rows[0]) if rows else None

    def find_alpha_by_key(self, key: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM alphas WHERE dedup_key = ?", (key,))
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------------ #
    # Simulations
    # ------------------------------------------------------------------ #
    def create_simulation(
        self,
        alpha_row_id: int,
        *,
        remote_simulation_id: str | None = None,
        status: str = SimulationStatus.PENDING,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO simulations (alpha_id, remote_simulation_id, status, submitted_at) "
            "VALUES (?, ?, ?, ?)",
            (alpha_row_id, remote_simulation_id, status, utcnow_iso()),
        )
        return int(cursor.lastrowid)

    def update_simulation(
        self,
        simulation_row_id: int,
        *,
        status: str | None = None,
        remote_simulation_id: str | None = None,
        remote_alpha_id: str | None = None,
        completed_at: str | None = None,
        error: str | None = None,
        mark_completed: bool = False,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if remote_simulation_id is not None:
            assignments.append("remote_simulation_id = ?")
            params.append(remote_simulation_id)
        if remote_alpha_id is not None:
            assignments.append("remote_alpha_id = ?")
            params.append(remote_alpha_id)
        if error is not None:
            assignments.append("error = ?")
            params.append(error)
        if completed_at is not None:
            assignments.append("completed_at = ?")
            params.append(completed_at)
        elif mark_completed:
            assignments.append("completed_at = ?")
            params.append(utcnow_iso())

        if not assignments:
            return
        params.append(simulation_row_id)
        self._execute(
            f"UPDATE simulations SET {', '.join(assignments)} WHERE id = ?", tuple(params)
        )

    def get_simulation(self, simulation_row_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM simulations WHERE id = ?", (simulation_row_id,))
        return dict(rows[0]) if rows else None

    def find_resumable_simulation(self, alpha_row_id: int) -> dict[str, Any] | None:
        """Latest simulation whose remote run may still be alive.

        This is what makes resume work: on restart we hand its
        ``remote_simulation_id`` back to the poller instead of resubmitting. A
        row without a remote id (the process died between INSERT and the POST
        returning) is also returned, so the caller can reuse it rather than
        leaving an orphan behind.
        """
        placeholders = ",".join("?" for _ in SimulationStatus.RESUMABLE)
        rows = self._query(
            f"SELECT * FROM simulations "
            f"WHERE alpha_id = ? AND status IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT 1",
            (alpha_row_id, *sorted(SimulationStatus.RESUMABLE)),
        )
        return dict(rows[0]) if rows else None

    def find_incomplete_simulations(self) -> list[dict[str, Any]]:
        """All simulations that may still have work pending, with alpha context.

        Used by ``--resume-only`` to finish in-flight work without needing the
        original input file.
        """
        placeholders = ",".join("?" for _ in SimulationStatus.RESUMABLE)
        rows = self._query(
            "SELECT s.id AS simulation_row_id, s.remote_simulation_id, s.status, "
            "       s.submitted_at, a.id AS alpha_row_id, a.dedup_key, a.name, "
            "       a.expression, a.settings_json "
            "FROM simulations s JOIN alphas a ON a.id = s.alpha_id "
            f"WHERE s.status IN ({placeholders}) ORDER BY s.id ASC",
            tuple(sorted(SimulationStatus.RESUMABLE)),
        )
        return [dict(row) for row in rows]

    def find_completed_result(self, key: str) -> AlphaResult | None:
        """Return the stored result of an already-finished alpha, if any."""
        alpha = self.find_alpha_by_key(key)
        if not alpha:
            return None
        rows = self._query(
            f"{_JOINED_SELECT} WHERE s.alpha_id = ? AND s.status = ? ORDER BY s.id DESC LIMIT 1",
            (int(alpha["id"]), SimulationStatus.COMPLETED),
        )
        if not rows:
            return None
        return self._row_to_result(rows[0], alpha)

    # ------------------------------------------------------------------ #
    # Results
    # ------------------------------------------------------------------ #
    def save_result(self, simulation_row_id: int, result: AlphaResult) -> None:
        """Persist parsed metrics plus the scrubbed raw payload."""
        raw_text: str | None = None
        if result.raw_json:
            try:
                payload = redact_sensitive(json.loads(result.raw_json))
            except json.JSONDecodeError:
                # Not JSON (an HTML error page, say). Keep it byte-for-byte:
                # the whole point of raw_json is that nothing is lost.
                raw_text = result.raw_json
            else:
                raw_text = json.dumps(payload, sort_keys=True)

        yearly_text = (
            json.dumps(result.yearly_stats, sort_keys=True) if result.yearly_stats else None
        )
        # The checks resolved by GET /alphas/{id}/check supersede the alpha
        # payload's copy, which reports SELF_CORRELATION as PENDING forever.
        # _row_to_result reads this column back as the submission verdict
        # whenever `submittable` is non-null, so the resolved set is what has to
        # be written here — storing the payload's copy would silently reload
        # every verified alpha as an unverified one.
        resolved_checks = result.submission_checks or result.checks
        checks_text = (
            json.dumps(resolved_checks, sort_keys=True) if resolved_checks else None
        )
        train_text = (
            json.dumps(result.train_stats, sort_keys=True) if result.train_stats else None
        )
        test_text = json.dumps(result.test_stats, sort_keys=True) if result.test_stats else None
        passed_value = None if result.passed is None else int(bool(result.passed))
        # None means the check never ran, which must stay distinguishable from
        # "ran and failed" — an unverified alpha is not a submittable one.
        submittable_value = (
            None if not result.submission_checks else int(bool(result.is_submittable))
        )

        self._execute(
            """
            INSERT INTO results (
                simulation_id, sharpe, fitness, turnover, returns, drawdown, margin,
                pnl, book_size, long_count, short_count, grade, stage,
                train_json, test_json, submittable, self_correlation, excluded_reason,
                yearly_stats_json, checks_json, raw_json, passed, reasons, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(simulation_id) DO UPDATE SET
                sharpe=excluded.sharpe, fitness=excluded.fitness, turnover=excluded.turnover,
                returns=excluded.returns, drawdown=excluded.drawdown, margin=excluded.margin,
                pnl=excluded.pnl, book_size=excluded.book_size,
                long_count=excluded.long_count, short_count=excluded.short_count,
                grade=excluded.grade, stage=excluded.stage,
                train_json=excluded.train_json, test_json=excluded.test_json,
                submittable=excluded.submittable, self_correlation=excluded.self_correlation,
                excluded_reason=excluded.excluded_reason,
                yearly_stats_json=excluded.yearly_stats_json, checks_json=excluded.checks_json,
                raw_json=excluded.raw_json, passed=excluded.passed, reasons=excluded.reasons
            """,
            (
                simulation_row_id, result.sharpe, result.fitness, result.turnover,
                result.returns, result.drawdown, result.margin, result.pnl,
                result.book_size, result.long_count, result.short_count,
                result.grade, result.stage,
                train_text, test_text, submittable_value, result.self_correlation,
                result.excluded_reason,
                yearly_text, checks_text, raw_text, passed_value,
                "; ".join(result.reasons), utcnow_iso(),
            ),
        )

    def record_submission_check(
        self,
        remote_alpha_id: str,
        checks: dict[str, Any],
        self_correlation: float | None = None,
    ) -> bool:
        """Attach a resolved submission verdict to an already-stored result.

        Two reasons this cannot just go through :meth:`save_result`:

        - Rows simulated before the check gate existed carry no verdict, and
          re-running them is not an option — the no-duplicate-alpha rule forbids
          submitting the same expression twice.
        - ``SELF_CORRELATION`` is not stable. It measures the alpha against
          everything else in the account, so an alpha that passed at 0.6996 can
          fail later simply because more alphas were added. Refreshing the
          verdict has to be possible without touching the simulation.

        Returns False when no completed row carries that alpha id.
        """
        rows = self._query(
            "SELECT r.id FROM results r "
            "JOIN simulations s ON s.id = r.simulation_id "
            "WHERE s.remote_alpha_id = ? AND s.status = ? "
            "ORDER BY r.id DESC LIMIT 1",
            (remote_alpha_id, SimulationStatus.COMPLETED),
        )
        if not rows:
            return False

        submittable = None if not checks else int(all_checks_passed(checks))
        self._execute(
            "UPDATE results SET checks_json = ?, submittable = ?, self_correlation = ? "
            "WHERE id = ?",
            (
                json.dumps(checks, sort_keys=True) if checks else None,
                submittable, self_correlation, rows[0]["id"],
            ),
        )
        return True

    def mark_excluded(
        self,
        remote_alpha_id: str,
        reason: str,
        *,
        clear: bool = False,
    ) -> bool:
        """Attach a permanent exclusion tag to a stored completed result.

        Used after a one-by-one screening pass (e.g. ``scripts/_screen``) has
        confirmed that an alpha cannot be submitted for a structural reason
        — `SELF_CORRELATION` permanently over the 0.7 limit against a sibling
        already in the account, a degenerate one-sided book, etc. The tag
        makes `rank_results` and the search's `--include-existing` pool skip
        the alpha so the operator never re-selects it from the leaderboard.

        Returns False when no completed row carries that alpha id.
        ``clear=True`` removes the tag instead of setting it.
        """
        rows = self._query(
            "SELECT r.id FROM results r "
            "JOIN simulations s ON s.id = r.simulation_id "
            "WHERE s.remote_alpha_id = ? AND s.status = ? "
            "ORDER BY r.id DESC LIMIT 1",
            (remote_alpha_id, SimulationStatus.COMPLETED),
        )
        if not rows:
            return False
        self._execute(
            "UPDATE results SET excluded_reason = ? WHERE id = ?",
            (None if clear else reason, rows[0]["id"]),
        )
        return True

    def record_yearly_stats(
        self,
        remote_alpha_id: str,
        yearly_stats: dict[str, Any],
        *,
        overwrite: bool = False,
    ) -> bool:
        """Attach a per-year recordset to an already-stored result.

        Exists for the same reason as :meth:`record_submission_check`: the yearly
        stats for most stored rows were silently dropped by a stage-label
        mismatch, and re-simulating them is not an option. The recordset stays
        available from the endpoint indefinitely, so it can simply be fetched
        again.

        An existing non-empty recordset is left alone unless ``overwrite`` is set,
        so a refresh cannot quietly replace data a later parse depended on.
        Returns False when no completed row carries that alpha id.
        """
        rows = self._query(
            "SELECT r.id, r.yearly_stats_json FROM results r "
            "JOIN simulations s ON s.id = r.simulation_id "
            "WHERE s.remote_alpha_id = ? AND s.status = ? "
            "ORDER BY r.id DESC LIMIT 1",
            (remote_alpha_id, SimulationStatus.COMPLETED),
        )
        if not rows:
            return False
        if rows[0]["yearly_stats_json"] and not overwrite:
            return False

        self._execute(
            "UPDATE results SET yearly_stats_json = ? WHERE id = ?",
            (
                json.dumps(yearly_stats, sort_keys=True) if yearly_stats else None,
                rows[0]["id"],
            ),
        )
        return True

    def _row_to_result(self, row: sqlite3.Row | dict[str, Any], alpha: dict[str, Any]) -> AlphaResult:
        data = dict(row)

        def load_json(key: str, default: Any) -> Any:
            raw = data.get(key)
            if not raw:
                return default
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return default

        reasons_raw = data.get("reasons")
        reasons = [part.strip() for part in str(reasons_raw).split(";") if part.strip()] if reasons_raw else []

        return AlphaResult(
            alpha_id=alpha.get("name") or alpha.get("dedup_key", "")[:16],
            expression=alpha.get("expression", ""),
            dedup_key=alpha.get("dedup_key", ""),
            status=data.get("status") or SimulationStatus.PENDING,
            simulation_id=data.get("remote_simulation_id"),
            remote_alpha_id=data.get("remote_alpha_id"),
            settings_json=alpha.get("settings_json", "{}"),
            sharpe=data.get("sharpe"),
            fitness=data.get("fitness"),
            turnover=data.get("turnover"),
            returns=data.get("returns"),
            drawdown=data.get("drawdown"),
            margin=data.get("margin"),
            pnl=data.get("pnl"),
            book_size=data.get("book_size"),
            long_count=data.get("long_count"),
            short_count=data.get("short_count"),
            yearly_stats=load_json("yearly_stats_json", {}),
            checks=load_json("checks_json", {}),
            raw_json=data.get("raw_json"),
            grade=data.get("grade"),
            stage=data.get("stage"),
            train_stats=load_json("train_json", None),
            test_stats=load_json("test_json", None),
            self_correlation=data.get("self_correlation"),
            # A null submittable means the check never ran, so the stored checks
            # came from the plain alpha payload where SELF_CORRELATION is still
            # PENDING — they must not be presented as a submission verdict.
            submission_checks=(
                load_json("checks_json", {}) if data.get("submittable") is not None else None
            ),
            created_at=alpha.get("created_at", ""),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            passed=None if data.get("passed") is None else bool(data.get("passed")),
            reasons=reasons,
            excluded_reason=data.get("excluded_reason"),
        )

    def all_results(self, *, statuses: Iterable[str] | None = None) -> list[AlphaResult]:
        """Every simulation joined with its alpha and result rows."""
        sql = _JOINED_SELECT + " "
        params: list[Any] = []
        status_list = list(statuses) if statuses else None
        if status_list:
            sql += f"WHERE s.status IN ({','.join('?' for _ in status_list)}) "
            params.extend(status_list)
        sql += "ORDER BY s.id ASC"

        results: list[AlphaResult] = []
        for row in self._query(sql, tuple(params)):
            alpha = {
                "dedup_key": row["dedup_key"],
                "name": row["name"],
                "expression": row["expression"],
                "settings_json": row["settings_json"],
                "created_at": row["created_at"],
            }
            results.append(self._row_to_result(row, alpha))
        return results

    def expression_already_simulated(self, expression: str) -> bool:
        """True when any simulation exists for this expression, whatever settings.

        Stricter than :meth:`find_alpha_by_key`, which keys on expression plus
        settings. Running one expression under two truncation values produced
        effectively identical alphas (both fitness 2.07) and wasted a simulation,
        so a search that must not repeat an alpha should check this instead.
        """
        rows = self._query(
            "SELECT 1 FROM alphas a JOIN simulations s ON s.alpha_id = a.id "
            "WHERE a.expression_hash = ? LIMIT 1",
            (expression_hash(expression),),
        )
        return bool(rows)

    def simulated_scope_hashes(self) -> set[str]:
        """Every ``expression + region/universe/delay`` already simulated.

        This is the search's skip rule. It is deliberately narrower than
        :func:`~worldquant.hashing.dedup_key` (which would allow a pure
        ``truncation`` tweak through) and wider than
        :func:`~worldquant.hashing.expression_hash` (which would block the same
        signal run over a different universe — a different alpha).

        Computed from the stored expression and settings rather than kept in a
        column, so no migration is needed and a change to ``SCOPE_SETTINGS``
        retroactively applies to the whole history.
        """
        hashes: set[str] = set()
        rows = self._query(
            "SELECT DISTINCT a.expression, a.settings_json FROM alphas a "
            "JOIN simulations s ON s.alpha_id = a.id"
        )
        for row in rows:
            try:
                settings = json.loads(row["settings_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                settings = {}
            hashes.add(scope_hash(str(row["expression"] or ""), settings))
        return hashes

    def find_by_grade(self, grade: str) -> list[AlphaResult]:
        """Completed results carrying a given BRAIN grade, newest first.

        Lets a grade-targeted search check what is already stored before
        spending any simulation quota.
        """
        rows = self._query(
            f"{_JOINED_SELECT} WHERE s.status = ? AND r.grade = ? ORDER BY s.id DESC",
            (SimulationStatus.COMPLETED, grade.strip().upper()),
        )
        results: list[AlphaResult] = []
        for row in rows:
            alpha = {
                "dedup_key": row["dedup_key"],
                "name": row["name"],
                "expression": row["expression"],
                "settings_json": row["settings_json"],
                "created_at": row["created_at"],
            }
            results.append(self._row_to_result(row, alpha))
        return results

    def grade_counts(self) -> dict[str, int]:
        """How many completed simulations landed on each grade."""
        counts: dict[str, int] = {}
        for row in self._query(
            "SELECT grade, COUNT(*) AS n FROM results "
            "WHERE grade IS NOT NULL GROUP BY grade ORDER BY n DESC"
        ):
            counts[str(row["grade"])] = int(row["n"])
        return counts

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._query("SELECT status, COUNT(*) AS n FROM simulations GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        return counts

    # ------------------------------------------------------------------ #
    # CSV export
    # ------------------------------------------------------------------ #
    def export_csv(self, path: str | Path, rows: Sequence[AlphaResult]) -> Path:
        out_path = Path(path)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" is required on Windows to avoid blank lines between rows.
            with out_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for result in rows:
                    writer.writerow(result.to_csv_row())
        except OSError as exc:
            raise StorageError(f"cannot write CSV {out_path}: {exc}") from exc
        return out_path

    def export_all(self, data_dir: str | Path, *, results: Sequence[AlphaResult] | None = None) -> dict[str, Path]:
        """Write ``results.csv``, ``passed.csv`` and ``failed.csv``.

        ``passed.csv`` holds alphas whose filter verdict was True; ``failed.csv``
        holds everything else that reached a terminal state, including runs that
        errored or timed out, so nothing disappears silently.
        """
        directory = Path(data_dir)
        rows = list(results) if results is not None else self.all_results()
        passed = [row for row in rows if row.passed is True]
        failed = [row for row in rows if row.passed is not True]

        return {
            "results": self.export_csv(directory / "results.csv", rows),
            "passed": self.export_csv(directory / "passed.csv", passed),
            "failed": self.export_csv(directory / "failed.csv", failed),
        }
