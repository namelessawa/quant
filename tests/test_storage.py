"""SQLite persistence, redaction and CSV export."""

from __future__ import annotations

import csv
import json

import pytest

from conftest import submission_checks as all_pass_checks
from worldquant.api import SimulationStatus
from worldquant.exceptions import StorageError
from worldquant.hashing import dedup_key, scope_hash
from worldquant.models import AlphaResult
from worldquant.storage import CSV_COLUMNS, ResultStore, redact_sensitive, utcnow_iso

SETTINGS = {"region": "USA", "delay": 1}


def make_result(expression="rank(close)", *, name="alpha_x", **overrides) -> AlphaResult:
    base = dict(
        alpha_id=name,
        expression=expression,
        dedup_key=dedup_key(expression, SETTINGS),
        status=SimulationStatus.COMPLETED,
        simulation_id="sim1",
        remote_alpha_id="Xp2Kd",
        settings_json=json.dumps(SETTINGS, sort_keys=True),
        sharpe=1.41,
        fitness=1.08,
        turnover=0.423,
        returns=0.12,
        drawdown=0.05,
        margin=0.0007,
        long_count=1500,
        short_count=1500,
        created_at=utcnow_iso(),
        completed_at=utcnow_iso(),
    )
    base.update(overrides)
    return AlphaResult(**base)


def seed(store, expression="rank(close)", *, status=SimulationStatus.COMPLETED, remote="sim1", **kw):
    alpha_row = store.upsert_alpha(expression, SETTINGS, name=kw.pop("name", "alpha_x"))
    sim_row = store.create_simulation(alpha_row, remote_simulation_id=remote, status=SimulationStatus.SUBMITTED)
    store.update_simulation(sim_row, status=status, remote_alpha_id="Xp2Kd", mark_completed=True)
    result = make_result(expression, status=status, **kw)
    store.save_result(sim_row, result)
    return alpha_row, sim_row, result


class TestSchema:
    def test_creates_the_three_documented_tables(self, store):
        names = {
            row["name"]
            for row in store._query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"alphas", "simulations", "results"} <= names

    def test_alphas_has_the_required_columns(self, store):
        columns = {row["name"] for row in store._query("PRAGMA table_info(alphas)")}
        assert {"id", "expression", "expression_hash", "settings_json", "created_at"} <= columns

    def test_simulations_has_the_required_columns(self, store):
        columns = {row["name"] for row in store._query("PRAGMA table_info(simulations)")}
        assert {
            "id", "alpha_id", "remote_simulation_id", "status",
            "submitted_at", "completed_at", "error",
        } <= columns

    def test_results_has_the_required_columns(self, store):
        columns = {row["name"] for row in store._query("PRAGMA table_info(results)")}
        assert {
            "simulation_id", "sharpe", "fitness", "turnover", "returns",
            "drawdown", "margin", "long_count", "short_count", "raw_json",
        } <= columns

    def test_opening_a_directory_as_a_database_raises_storage_error(self, tmp_path):
        with pytest.raises(StorageError):
            ResultStore(tmp_path)

    def test_schema_is_idempotent_across_reopens(self, tmp_path):
        path = tmp_path / "reopen.db"
        with ResultStore(path) as first:
            seed(first)
        with ResultStore(path) as second:
            assert len(second.all_results()) == 1


class TestAlphaUpsert:
    def test_same_expression_and_settings_reuse_one_row(self, store):
        first = store.upsert_alpha("rank( close )", SETTINGS, name="a")
        second = store.upsert_alpha("rank(close)", SETTINGS, name="a")
        assert first == second
        assert len(store._query("SELECT id FROM alphas")) == 1

    def test_different_settings_create_a_second_row(self, store):
        first = store.upsert_alpha("rank(close)", SETTINGS)
        second = store.upsert_alpha("rank(close)", {"region": "CHN", "delay": 1})
        assert first != second

    def test_expression_hash_is_stored(self, store):
        from worldquant.hashing import expression_hash

        row_id = store.upsert_alpha("rank(close)", SETTINGS)
        row = store.get_alpha(row_id)
        assert row["expression_hash"] == expression_hash("rank(close)")

    def test_later_name_fills_an_unnamed_row(self, store):
        row_id = store.upsert_alpha("rank(close)", SETTINGS)
        assert store.get_alpha(row_id)["name"] is None
        store.upsert_alpha("rank(close)", SETTINGS, name="filled_in")
        assert store.get_alpha(row_id)["name"] == "filled_in"


class TestSimulationLifecycle:
    def test_status_transitions_are_recorded(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row)
        assert store.get_simulation(sim_row)["status"] == SimulationStatus.PENDING

        store.update_simulation(sim_row, status=SimulationStatus.SUBMITTED, remote_simulation_id="r1")
        row = store.get_simulation(sim_row)
        assert row["status"] == SimulationStatus.SUBMITTED
        assert row["remote_simulation_id"] == "r1"
        assert row["submitted_at"]

        store.update_simulation(sim_row, status=SimulationStatus.RUNNING)
        assert store.get_simulation(sim_row)["status"] == SimulationStatus.RUNNING

        store.update_simulation(sim_row, status=SimulationStatus.COMPLETED, mark_completed=True)
        row = store.get_simulation(sim_row)
        assert row["status"] == SimulationStatus.COMPLETED
        assert row["completed_at"]

    def test_error_message_is_persisted(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="r1")
        store.update_simulation(
            sim_row, status=SimulationStatus.FAILED, error="unit handling mismatch", mark_completed=True
        )
        row = store.get_simulation(sim_row)
        assert row["status"] == SimulationStatus.FAILED
        assert row["error"] == "unit handling mismatch"

    def test_update_without_fields_is_a_no_op(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row)
        before = store.get_simulation(sim_row)
        store.update_simulation(sim_row)
        assert dict(store.get_simulation(sim_row)) == dict(before)

    def test_find_resumable_returns_the_latest_non_terminal(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        old = store.create_simulation(alpha_row, remote_simulation_id="old")
        store.update_simulation(old, status=SimulationStatus.FAILED, mark_completed=True)
        recent = store.create_simulation(alpha_row, remote_simulation_id="recent")
        store.update_simulation(recent, status=SimulationStatus.RUNNING)

        resumable = store.find_resumable_simulation(alpha_row)
        assert resumable["remote_simulation_id"] == "recent"

    def test_find_resumable_is_none_when_everything_is_terminal(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="done")
        store.update_simulation(sim_row, status=SimulationStatus.COMPLETED, mark_completed=True)
        assert store.find_resumable_simulation(alpha_row) is None

    def test_find_incomplete_lists_pending_and_running_only(self, store):
        a1 = store.upsert_alpha("rank(close)", SETTINGS)
        a2 = store.upsert_alpha("rank(volume)", SETTINGS)
        a3 = store.upsert_alpha("rank(returns)", SETTINGS)
        s1 = store.create_simulation(a1, remote_simulation_id="r1")
        store.update_simulation(s1, status=SimulationStatus.COMPLETED, mark_completed=True)
        store.create_simulation(a2, remote_simulation_id="r2")
        s3 = store.create_simulation(a3)
        store.update_simulation(s3, status=SimulationStatus.RUNNING)

        incomplete = store.find_incomplete_simulations()
        assert {row["remote_simulation_id"] for row in incomplete} == {"r2", None}
        assert all(row["expression"] for row in incomplete)


class TestRawJsonRedaction:
    def test_sensitive_keys_are_redacted_before_persisting(self, store):
        raw = {
            "id": "Xp2Kd",
            "is": {"sharpe": 1.4},
            "password": "hunter2",
            "token": "abc",
            "nested": {"cookie": "session=xyz", "sharpe": 1.0},
        }
        _, _, _ = seed(store, raw_json=None)
        sim_row = int(store._query("SELECT id FROM simulations")[0]["id"])
        store.save_result(sim_row, make_result(raw_json=json.dumps(raw)))

        stored = json.loads(store.all_results()[0].raw_json)
        assert stored["password"] == "<redacted>"
        assert stored["token"] == "<redacted>"
        assert stored["nested"]["cookie"] == "<redacted>"
        # Non-sensitive payload is preserved verbatim for later re-parsing.
        assert stored["is"]["sharpe"] == 1.4
        assert "hunter2" not in json.dumps(stored)

    def test_redact_sensitive_handles_lists_and_depth(self):
        payload = {"a": [{"secret": "x", "keep": 1}], "b": "ok"}
        scrubbed = redact_sensitive(payload)
        assert scrubbed["a"][0]["secret"] == "<redacted>"
        assert scrubbed["a"][0]["keep"] == 1
        assert scrubbed["b"] == "ok"

    def test_unparseable_raw_json_is_still_kept(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="r1")
        store.update_simulation(sim_row, status=SimulationStatus.COMPLETED, mark_completed=True)
        store.save_result(sim_row, make_result(raw_json="not json at all"))
        assert store.all_results()[0].raw_json == "not json at all"


class TestResultRoundTrip:
    def test_metrics_survive_a_round_trip(self, store):
        seed(store)
        loaded = store.all_results()[0]
        assert loaded.sharpe == pytest.approx(1.41)
        assert loaded.fitness == pytest.approx(1.08)
        assert loaded.turnover == pytest.approx(0.423)
        assert loaded.long_count == 1500
        assert loaded.remote_alpha_id == "Xp2Kd"
        assert loaded.expression == "rank(close)"

    def test_yearly_stats_round_trip(self, store):
        yearly = {"2019": {"sharpe": 1.1}, "2020": {"sharpe": -0.4}}
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="r1")
        store.update_simulation(sim_row, status=SimulationStatus.COMPLETED, mark_completed=True)
        store.save_result(sim_row, make_result(yearly_stats=yearly))

        loaded = store.all_results()[0]
        assert loaded.yearly_stats == yearly
        assert loaded.yearly_summary.positive_years == 1
        assert loaded.yearly_summary.negative_years == 1

    def test_save_result_is_upsert_not_duplicate(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="r1")
        store.save_result(sim_row, make_result(sharpe=1.0))
        store.save_result(sim_row, make_result(sharpe=2.0))
        rows = store.all_results()
        assert len(rows) == 1
        assert rows[0].sharpe == pytest.approx(2.0)

    def test_failed_simulation_has_no_metrics_but_keeps_its_error(self, store):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS)
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="r1")
        store.update_simulation(
            sim_row, status=SimulationStatus.FAILED, error="parse error", mark_completed=True
        )
        store.save_result(
            sim_row,
            make_result(status=SimulationStatus.FAILED, sharpe=None, passed=False,
                        reasons=["Status FAILED != COMPLETED (parse error)"]),
        )
        loaded = store.all_results()[0]
        assert loaded.status == SimulationStatus.FAILED
        assert loaded.sharpe is None
        assert loaded.error == "parse error"
        assert loaded.passed is False
        assert loaded.reasons == ["Status FAILED != COMPLETED (parse error)"]

    def test_find_completed_result_matches_on_expression_and_settings(self, store):
        seed(store)
        key = dedup_key("rank( close )", SETTINGS)
        found = store.find_completed_result(key)
        assert found is not None
        assert found.remote_alpha_id == "Xp2Kd"

    def test_find_completed_result_ignores_failed_runs(self, store):
        seed(store, status=SimulationStatus.FAILED)
        assert store.find_completed_result(dedup_key("rank(close)", SETTINGS)) is None

    def test_find_completed_result_for_unknown_key(self, store):
        assert store.find_completed_result("nope") is None

    def test_status_counts(self, store):
        seed(store, "rank(close)", status=SimulationStatus.COMPLETED, remote="r1")
        a2 = store.upsert_alpha("rank(volume)", SETTINGS)
        s2 = store.create_simulation(a2, remote_simulation_id="r2")
        store.update_simulation(s2, status=SimulationStatus.FAILED, mark_completed=True)
        assert store.status_counts() == {
            SimulationStatus.COMPLETED: 1,
            SimulationStatus.FAILED: 1,
        }


class TestGradeStorage:
    def _seed_with_grade(self, store, expression, grade, *, status=SimulationStatus.COMPLETED):
        alpha_row = store.upsert_alpha(expression, SETTINGS, name=expression[:12])
        sim_row = store.create_simulation(alpha_row, remote_simulation_id=f"sim_{expression}")
        store.update_simulation(
            sim_row, status=status, remote_alpha_id=f"A_{expression}", mark_completed=True
        )
        store.save_result(
            sim_row,
            make_result(expression, status=status, grade=grade, stage="IS", sharpe=1.4),
        )
        return sim_row

    def test_grade_and_stage_round_trip(self, store):
        self._seed_with_grade(store, "rank(close)", "AVERAGE")
        loaded = store.all_results()[0]
        assert loaded.grade == "AVERAGE"
        assert loaded.stage == "IS"

    def test_results_table_has_grade_columns(self, store):
        columns = {row["name"] for row in store._query("PRAGMA table_info(results)")}
        assert {"grade", "stage"} <= columns

    def test_database_from_before_the_columns_existed_is_migrated(self, tmp_path):
        # CREATE TABLE IF NOT EXISTS never alters an existing table, so a
        # database written by an older version would break every query naming
        # grade. The additive migration must back-fill it instead.
        import sqlite3

        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(str(path))
        legacy.executescript(
            """
            CREATE TABLE alphas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE,
                name TEXT, expression TEXT NOT NULL, expression_hash TEXT NOT NULL,
                settings_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alpha_id INTEGER NOT NULL REFERENCES alphas(id),
                remote_simulation_id TEXT, remote_alpha_id TEXT, status TEXT NOT NULL,
                submitted_at TEXT, completed_at TEXT, error TEXT);
            CREATE TABLE results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                simulation_id INTEGER NOT NULL UNIQUE REFERENCES simulations(id),
                sharpe REAL, fitness REAL, turnover REAL, returns REAL, drawdown REAL,
                margin REAL, pnl REAL, book_size REAL, long_count INTEGER,
                short_count INTEGER, yearly_stats_json TEXT, checks_json TEXT,
                raw_json TEXT, passed INTEGER, reasons TEXT, created_at TEXT NOT NULL);
            """
        )
        legacy.commit()
        legacy.close()

        with ResultStore(path) as migrated:
            columns = {row["name"] for row in migrated._query("PRAGMA table_info(results)")}
            assert {"grade", "stage"} <= columns
            # And the store is fully usable afterwards.
            self._seed_with_grade(migrated, "rank(close)", "GOOD")
            assert migrated.all_results()[0].grade == "GOOD"

    def test_migration_is_idempotent(self, tmp_path):
        path = tmp_path / "twice.db"
        with ResultStore(path) as first:
            self._seed_with_grade(first, "rank(close)", "AVERAGE")
        with ResultStore(path) as second:
            assert second.all_results()[0].grade == "AVERAGE"

    def test_find_by_grade_matches_only_that_grade(self, store):
        self._seed_with_grade(store, "rank(close)", "INFERIOR")
        self._seed_with_grade(store, "rank(volume)", "AVERAGE")
        self._seed_with_grade(store, "rank(returns)", "AVERAGE")

        found = store.find_by_grade("AVERAGE")
        assert len(found) == 2
        assert all(r.grade == "AVERAGE" for r in found)

    def test_find_by_grade_is_case_insensitive(self, store):
        self._seed_with_grade(store, "rank(close)", "AVERAGE")
        assert len(store.find_by_grade(" average ")) == 1

    def test_find_by_grade_ignores_incomplete_runs(self, store):
        self._seed_with_grade(store, "rank(close)", "AVERAGE", status=SimulationStatus.FAILED)
        assert store.find_by_grade("AVERAGE") == []

    def test_find_by_grade_returns_newest_first(self, store):
        self._seed_with_grade(store, "rank(close)", "AVERAGE")
        self._seed_with_grade(store, "rank(volume)", "AVERAGE")
        assert store.find_by_grade("AVERAGE")[0].expression == "rank(volume)"

    def test_grade_counts(self, store):
        self._seed_with_grade(store, "rank(close)", "INFERIOR")
        self._seed_with_grade(store, "rank(volume)", "AVERAGE")
        self._seed_with_grade(store, "rank(returns)", "AVERAGE")
        assert store.grade_counts() == {"AVERAGE": 2, "INFERIOR": 1}

    def test_grade_counts_is_empty_without_grades(self, store):
        seed(store)
        assert store.grade_counts() == {}

    def test_grade_reaches_the_csv(self, store, tmp_path):
        self._seed_with_grade(store, "rank(close)", "AVERAGE")
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["grade"] == "AVERAGE"
        assert row["stage"] == "IS"


class TestPeriodStorage:
    """The mandatory one-year test period must survive a database round trip."""

    TRAIN = {"sharpe": -0.53, "fitness": -0.18, "start_date": "2019-01-01"}
    TEST = {"sharpe": 0.42, "fitness": 0.13, "start_date": "2022-12-31"}

    def _seed(self, store, **overrides):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="a")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim1")
        store.update_simulation(
            sim_row, status=SimulationStatus.COMPLETED, remote_alpha_id="A1",
            mark_completed=True,
        )
        store.save_result(sim_row, make_result(**overrides))
        return store.all_results()[0]

    def test_train_and_test_round_trip(self, store):
        loaded = self._seed(store, train_stats=self.TRAIN, test_stats=self.TEST)
        assert loaded.train_stats == self.TRAIN
        assert loaded.test_stats == self.TEST

    def test_results_table_has_period_columns(self, store):
        columns = {row["name"] for row in store._query("PRAGMA table_info(results)")}
        assert {"train_json", "test_json"} <= columns

    def test_absent_periods_stay_none(self, store):
        loaded = self._seed(store)
        assert loaded.train_stats is None
        assert loaded.test_stats is None

    def test_database_from_before_the_columns_existed_is_migrated(self, tmp_path):
        import sqlite3

        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(str(path))
        legacy.executescript(
            """
            CREATE TABLE alphas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE,
                name TEXT, expression TEXT NOT NULL, expression_hash TEXT NOT NULL,
                settings_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alpha_id INTEGER NOT NULL REFERENCES alphas(id),
                remote_simulation_id TEXT, remote_alpha_id TEXT, status TEXT NOT NULL,
                submitted_at TEXT, completed_at TEXT, error TEXT);
            CREATE TABLE results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                simulation_id INTEGER NOT NULL UNIQUE REFERENCES simulations(id),
                sharpe REAL, fitness REAL, turnover REAL, returns REAL, drawdown REAL,
                margin REAL, pnl REAL, book_size REAL, long_count INTEGER,
                short_count INTEGER, grade TEXT, stage TEXT, yearly_stats_json TEXT,
                checks_json TEXT, raw_json TEXT, passed INTEGER, reasons TEXT,
                created_at TEXT NOT NULL);
            """
        )
        legacy.commit()
        legacy.close()

        with ResultStore(path) as migrated:
            columns = {row["name"] for row in migrated._query("PRAGMA table_info(results)")}
            assert {"train_json", "test_json"} <= columns
            assert self._seed(migrated, test_stats=self.TEST).test_stats == self.TEST


class TestSubmissionCheckStorage:
    """The 8-check verdict must survive a round trip, or every stored alpha
    reloads as unverified and --include-existing can never report a hit."""

    #: What the plain alpha payload carries: SELF_CORRELATION never resolves there.
    PAYLOAD_CHECKS = {
        "LOW_SHARPE": {"value": 1.41, "result": "PASS", "limit": 1.25},
        "SELF_CORRELATION": {"value": None, "result": "PENDING", "limit": 0.7},
    }

    def _seed(self, store, **overrides):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="a")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim1")
        store.update_simulation(
            sim_row, status=SimulationStatus.COMPLETED, remote_alpha_id="A1",
            mark_completed=True,
        )
        store.save_result(sim_row, make_result(**overrides))
        return store.all_results()[0]

    def test_results_table_has_the_verdict_columns(self, store):
        columns = {row["name"] for row in store._query("PRAGMA table_info(results)")}
        assert {"submittable", "self_correlation"} <= columns

    def test_a_verified_alpha_round_trips(self, store):
        checks = all_pass_checks()
        loaded = self._seed(store, submission_checks=checks, self_correlation=0.6996)
        assert loaded.submission_checks == checks
        assert loaded.is_submittable is True
        assert loaded.self_correlation == pytest.approx(0.6996)

    def test_a_failed_check_round_trips_with_its_reason(self, store):
        checks = all_pass_checks({"SELF_CORRELATION": "FAIL"})
        loaded = self._seed(store, submission_checks=checks, self_correlation=0.81)
        assert loaded.is_submittable is False
        assert loaded.submission_failures == ["SELF_CORRELATION: FAIL"]
        assert loaded.self_correlation == pytest.approx(0.81)

    def test_the_resolved_checks_supersede_the_alpha_payload_copy(self, store):
        # Regression: only `checks` used to be written, so a verified alpha came
        # back with SELF_CORRELATION still PENDING and read as unverified.
        loaded = self._seed(
            store, checks=self.PAYLOAD_CHECKS, submission_checks=all_pass_checks(),
        )
        assert loaded.submission_checks is not None
        assert loaded.is_submittable is True
        assert loaded.submission_checks["SELF_CORRELATION"]["result"] == "PASS"

    def test_never_checked_stays_distinguishable_from_failed(self, store):
        loaded = self._seed(store, checks=self.PAYLOAD_CHECKS)
        assert loaded.submission_checks is None
        assert loaded.is_submittable is False
        row = store._query("SELECT submittable FROM results")[0]
        assert row["submittable"] is None, "NULL means the check never ran"

    def test_a_failed_verdict_is_stored_as_zero_not_null(self, store):
        self._seed(store, submission_checks=all_pass_checks({"LOW_FITNESS": "FAIL"}))
        row = store._query("SELECT submittable FROM results")[0]
        assert row["submittable"] == 0

    def test_backfill_attaches_a_verdict_to_an_unchecked_row(self, store):
        # Rows simulated before the gate existed carry no verdict, and the
        # no-duplicate-alpha rule means they cannot simply be re-run.
        self._seed(store, checks=self.PAYLOAD_CHECKS)
        assert store.all_results()[0].submission_checks is None

        assert store.record_submission_check("A1", all_pass_checks(), 0.42) is True

        reloaded = store.all_results()[0]
        assert reloaded.is_submittable is True
        assert reloaded.self_correlation == pytest.approx(0.42)

    def test_backfill_recomputes_the_verdict_itself(self, store):
        self._seed(store)
        store.record_submission_check(
            "A1", all_pass_checks({"SELF_CORRELATION": "FAIL"}), 0.81
        )
        reloaded = store.all_results()[0]
        assert reloaded.is_submittable is False
        assert reloaded.submission_failures == ["SELF_CORRELATION: FAIL"]

    def test_backfill_overwrites_a_stale_correlation(self, store):
        # SELF_CORRELATION measures against the whole account, so it drifts as
        # more alphas are added; a refresh has to replace the old number.
        self._seed(store, submission_checks=all_pass_checks(), self_correlation=0.31)
        store.record_submission_check(
            "A1", all_pass_checks({"SELF_CORRELATION": "FAIL"}), 0.74
        )
        reloaded = store.all_results()[0]
        assert reloaded.self_correlation == pytest.approx(0.74)
        assert reloaded.is_submittable is False

    def test_backfill_of_an_unknown_alpha_id_reports_failure(self, store):
        self._seed(store)
        assert store.record_submission_check("NOPE", all_pass_checks()) is False

    def test_backfill_with_no_checks_leaves_the_row_unverified(self, store):
        self._seed(store, submission_checks=all_pass_checks(), self_correlation=0.31)
        store.record_submission_check("A1", {})
        assert store.all_results()[0].submission_checks is None

    def test_database_from_before_the_columns_existed_is_migrated(self, tmp_path):
        import sqlite3

        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(str(path))
        legacy.executescript(
            """
            CREATE TABLE alphas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE,
                name TEXT, expression TEXT NOT NULL, expression_hash TEXT NOT NULL,
                settings_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alpha_id INTEGER NOT NULL REFERENCES alphas(id),
                remote_simulation_id TEXT, remote_alpha_id TEXT, status TEXT NOT NULL,
                submitted_at TEXT, completed_at TEXT, error TEXT);
            CREATE TABLE results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                simulation_id INTEGER NOT NULL UNIQUE REFERENCES simulations(id),
                sharpe REAL, fitness REAL, turnover REAL, returns REAL, drawdown REAL,
                margin REAL, pnl REAL, book_size REAL, long_count INTEGER,
                short_count INTEGER, grade TEXT, stage TEXT, train_json TEXT,
                test_json TEXT, yearly_stats_json TEXT, checks_json TEXT,
                raw_json TEXT, passed INTEGER, reasons TEXT, created_at TEXT NOT NULL);
            """
        )
        legacy.commit()
        legacy.close()

        with ResultStore(path) as migrated:
            columns = {row["name"] for row in migrated._query("PRAGMA table_info(results)")}
            assert {"submittable", "self_correlation"} <= columns
            seeded = self._seed(migrated, submission_checks=all_pass_checks())
            assert seeded.is_submittable is True


class TestYearlyStatsBackfill:
    """Repair path for rows whose yearly stats were silently dropped."""

    STATS = {"2019": {"sharpe": 3.06}, "2020": {"sharpe": 1.44}}

    def _seed(self, store, **overrides):
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="a")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim1")
        store.update_simulation(
            sim_row, status=SimulationStatus.COMPLETED, remote_alpha_id="A1",
            mark_completed=True,
        )
        store.save_result(sim_row, make_result(**overrides))
        return store.all_results()[0]

    def test_fills_an_empty_recordset(self, store):
        self._seed(store)
        assert store.all_results()[0].yearly_stats == {}

        assert store.record_yearly_stats("A1", self.STATS) is True
        assert store.all_results()[0].yearly_stats == self.STATS

    def test_does_not_overwrite_existing_data_by_default(self, store):
        existing = {"2021": {"sharpe": 0.9}}
        self._seed(store, yearly_stats=existing)
        assert store.record_yearly_stats("A1", self.STATS) is False
        assert store.all_results()[0].yearly_stats == existing

    def test_overwrites_when_explicitly_asked(self, store):
        self._seed(store, yearly_stats={"2021": {"sharpe": 0.9}})
        assert store.record_yearly_stats("A1", self.STATS, overwrite=True) is True
        assert store.all_results()[0].yearly_stats == self.STATS

    def test_an_unknown_alpha_id_reports_failure(self, store):
        self._seed(store)
        assert store.record_yearly_stats("NOPE", self.STATS) is False

    def test_the_summary_is_recomputed_from_the_backfilled_rows(self, store):
        self._seed(store)
        store.record_yearly_stats("A1", {"2019": {"sharpe": 1.4}, "2020": {"sharpe": -0.6}})
        summary = store.all_results()[0].yearly_summary
        assert summary.total_years == 2
        assert summary.positive_years == 1
        assert summary.worst_year_sharpe == pytest.approx(-0.6)


class TestExpressionDedup:
    """Two dedup granularities.

    ``expression_already_simulated`` answers "has this exact expression ever been
    simulated". The search's skip rule is the narrower :meth:`simulated_scope_hashes`:
    expression plus region/universe/delay.
    """

    def test_unseen_expression_is_not_flagged(self, store):
        assert store.expression_already_simulated("rank(close)") is False

    def test_simulated_expression_is_flagged(self, store):
        seed(store, "rank(close)")
        assert store.expression_already_simulated("rank(close)") is True

    def test_flagged_regardless_of_settings(self, store):
        # The looser dedup_key let one expression run twice under different
        # truncation values and produce effectively identical alphas.
        seed(store, "rank(close)")
        other_row = store.upsert_alpha("rank(close)", {"region": "CHN", "delay": 0})
        original_row = store.find_alpha_by_key(dedup_key("rank(close)", SETTINGS))
        assert other_row != original_row["id"], "settings must still make distinct rows"
        assert store.expression_already_simulated("rank(close)") is True

    def test_whitespace_variants_are_the_same_expression(self, store):
        seed(store, "rank(ts_delta(close, 5))")
        assert store.expression_already_simulated("rank( ts_delta(close,5) )") is True

    def test_different_expression_is_not_flagged(self, store):
        seed(store, "rank(close)")
        assert store.expression_already_simulated("rank(volume)") is False

    def test_scope_hashes_come_back_in_one_call(self, store):
        seed(store, "rank(close)")
        seed(store, "rank(volume)")
        hashes = store.simulated_scope_hashes()
        assert len(hashes) == 2
        assert scope_hash("rank(close)", SETTINGS) in hashes
        assert scope_hash("rank(volume)", SETTINGS) in hashes

    def test_empty_database_has_no_scope_hashes(self, store):
        assert store.simulated_scope_hashes() == set()

    def test_another_universe_or_delay_is_a_new_scope(self, store):
        # The point of the narrower rule: the same signal over a different
        # instrument set is a different alpha, so a sweep must not be blocked.
        seed(store, "rank(close)")
        hashes = store.simulated_scope_hashes()
        assert scope_hash("rank(close)", {**SETTINGS, "universe": "TOP1000"}) not in hashes
        assert scope_hash("rank(close)", {**SETTINGS, "delay": 0}) not in hashes
        assert scope_hash("rank(close)", {**SETTINGS, "region": "CHN"}) not in hashes

    def test_a_construction_setting_does_not_make_a_new_scope(self, store):
        # One expression under two truncation values once produced effectively
        # identical alphas (both fitness 2.07), so those still collapse.
        seed(store, "rank(close)")
        hashes = store.simulated_scope_hashes()
        for tweak in ({"truncation": 0.05}, {"decay": 16}, {"neutralization": "NONE"},
                      {"nan_handling": "ON"}, {"testPeriod": "P2Y"}):
            assert scope_hash("rank(close)", {**SETTINGS, **tweak}) in hashes, tweak

    def test_whitespace_variants_share_a_scope(self, store):
        seed(store, "rank(ts_delta(close, 5))")
        hashes = store.simulated_scope_hashes()
        assert scope_hash("rank( ts_delta(close,5) )", SETTINGS) in hashes

    def test_a_missing_universe_falls_back_to_the_default(self, store):
        # SETTINGS carries no universe, so both sides normalize to the default
        # and must agree — otherwise every stored row would look unsimulated.
        seed(store, "rank(close)")
        hashes = store.simulated_scope_hashes()
        assert scope_hash("rank(close)", {**SETTINGS, "universe": "TOP3000"}) in hashes

    def test_malformed_stored_settings_do_not_lose_the_whole_set(self, store):
        seed(store, "rank(close)")
        seed(store, "rank(volume)")
        store._execute("UPDATE alphas SET settings_json = 'not json' WHERE expression = ?",
                       ("rank(volume)",))
        hashes = store.simulated_scope_hashes()
        assert scope_hash("rank(close)", SETTINGS) in hashes
        # The broken row still contributes a hash, computed from empty settings.
        assert scope_hash("rank(volume)", {}) in hashes


class TestCsvExport:
    def test_exports_the_three_files(self, store, tmp_path):
        seed(store)
        paths = store.export_all(tmp_path)
        assert set(paths) == {"results", "passed", "failed"}
        for path in paths.values():
            assert path.exists()

    def test_header_matches_the_documented_columns(self, store, tmp_path):
        seed(store)
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
        assert header == list(CSV_COLUMNS)

    def test_passed_and_failed_are_split(self, store, tmp_path):
        a1 = store.upsert_alpha("rank(close)", SETTINGS, name="good")
        s1 = store.create_simulation(a1, remote_simulation_id="r1")
        store.update_simulation(s1, status=SimulationStatus.COMPLETED, mark_completed=True)
        store.save_result(s1, make_result("rank(close)", name="good", passed=True, reasons=[]))

        a2 = store.upsert_alpha("rank(volume)", SETTINGS, name="bad")
        s2 = store.create_simulation(a2, remote_simulation_id="r2")
        store.update_simulation(s2, status=SimulationStatus.COMPLETED, mark_completed=True)
        store.save_result(
            s2, make_result("rank(volume)", name="bad", sharpe=0.2, passed=False,
                            reasons=["Sharpe 0.20 < 1.25"])
        )

        paths = store.export_all(tmp_path)
        with paths["passed"].open(encoding="utf-8", newline="") as handle:
            passed_rows = list(csv.DictReader(handle))
        with paths["failed"].open(encoding="utf-8", newline="") as handle:
            failed_rows = list(csv.DictReader(handle))

        assert [row["alpha_id"] for row in passed_rows] == ["good"]
        assert [row["alpha_id"] for row in failed_rows] == ["bad"]
        assert failed_rows[0]["reasons"] == "Sharpe 0.20 < 1.25"

    def test_yearly_columns_are_present(self, store, tmp_path):
        seed(store, yearly_stats={"2019": {"sharpe": 1.1}, "2020": {"sharpe": -0.4}})
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["positive_years"] == "1"
        assert row["negative_years"] == "1"
        assert row["total_years"] == "2"
        assert float(row["positive_year_ratio"]) == pytest.approx(0.5)
        assert json.loads(row["yearly_stats_json"])["2019"]["sharpe"] == 1.1

    def test_submission_verdict_reaches_the_csv(self, store, tmp_path):
        seed(store, submission_checks=all_pass_checks(), self_correlation=0.6996)
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["submittable"] == "YES"
        assert row["checks_passed"] == "8/8"
        assert row["checks_failed"] == ""
        assert float(row["self_correlation"]) == pytest.approx(0.6996)

    def test_a_failed_check_names_what_is_blocking(self, store, tmp_path):
        checks = all_pass_checks({"SELF_CORRELATION": "FAIL"})
        seed(store, submission_checks=checks, self_correlation=0.81)
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["submittable"] == "NO"
        assert row["checks_passed"] == "7/8"
        assert "SELF_CORRELATION" in row["checks_failed"]

    def test_never_checked_leaves_the_verdict_blank(self, store, tmp_path):
        # Blank, not NO: passed.csv can hold a factor that cleared every
        # configured threshold yet was never checked, and a cell reading NO would
        # claim a verdict nobody obtained.
        seed(store)
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        for column in ("submittable", "checks_passed", "self_correlation"):
            assert row[column] == "", column

    def test_the_verdict_columns_are_declared(self):
        assert {"submittable", "checks_passed", "checks_failed", "self_correlation"} <= set(
            CSV_COLUMNS
        )

    def test_export_of_an_empty_database_writes_headers_only(self, store, tmp_path):
        paths = store.export_all(tmp_path)
        with paths["results"].open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        assert len(rows) == 1

    def test_export_to_an_unwritable_path_raises_storage_error(self, store, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory", encoding="utf-8")
        with pytest.raises(StorageError):
            store.export_csv(blocker / "nested" / "results.csv", [])

    def test_no_blank_lines_between_rows_on_windows(self, store, tmp_path):
        seed(store)
        paths = store.export_all(tmp_path)
        content = paths["results"].read_text(encoding="utf-8")
        assert "\r\n\r\n" not in content and "\n\n" not in content
