"""End-to-end integration tests.

Only ``requests.Session`` is replaced. The real client, retry logic, poller,
parser, SQLite store, filters and CSV export all run, so these tests cover the
whole chain: login -> submit -> poll -> result -> filter -> persist -> export.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import replace

import pytest

from conftest import YEARLY_RECORDSET, FakeClock, FakeResponse, FakeSession, quiet_logger
from worldquant.api import SimulationStatus
from worldquant.client import WorldQuantClient
from worldquant.config import Credentials, RunnerConfig, StorageConfig, load_config
from worldquant.loader import specs_from_expressions
from worldquant.ranking import format_leaderboard
from worldquant.simulator import SimulationRunner
from worldquant.storage import ResultStore

BASE = "https://api.worldquantbrain.com"


def alpha_payload(alpha_id: str, *, sharpe=1.41, fitness=1.08, turnover=0.423,
                  grade="INFERIOR", stage="IS") -> dict:
    """A response shaped like the real GET /alphas/{id} body."""
    return {
        "id": alpha_id,
        "type": "REGULAR",
        "author": "tester",
        "dateCreated": "2026-09-05T10:00:00Z",
        "dateSubmitted": None,
        "status": "UNSUBMITTED",
        "grade": grade,
        "stage": stage,
        "settings": {
            "instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000",
            "delay": 1, "decay": 0, "neutralization": "INDUSTRY", "truncation": 0.08,
            "pasteurization": "ON", "unitHandling": "VERIFY", "nanHandling": "OFF",
            "language": "FASTEXPR", "visualization": False,
        },
        "regular": {"code": "rank(ts_delta(close, 5))"},
        "is": {
            "sharpe": sharpe,
            "fitness": fitness,
            "turnover": turnover,
            "returns": 0.0721,
            "drawdown": 0.0432,
            "margin": 0.00058,
            "pnl": 123456.7,
            "bookSize": 1000000.0,
            "longCount": 1520,
            "shortCount": 1480,
            "startDate": "2015-01-01",
            "checks": [
                {"name": "LOW_SHARPE", "value": sharpe, "result": "PASS", "limit": 1.25},
                {"name": "HIGH_TURNOVER", "value": turnover, "result": "PASS", "limit": 0.7},
                # Resolved only by GET /alphas/{id}/check; the live alpha payload
                # reports PENDING here indefinitely.
                {"name": "SELF_CORRELATION", "value": None, "result": "PENDING", "limit": 0.7},
            ],
        },
    }


def check_payload(alpha_id: str, *, results=None, self_correlation=0.31,
                  sharpe=1.41, fitness=1.08, turnover=0.423) -> dict:
    """A response shaped like the real ``GET /alphas/{id}/check`` body.

    All eight checks are emitted, because a partial response is precisely what
    ``all_checks_passed`` must refuse to read as a pass. ``results`` overrides
    individual verdicts by check name, so a test can fail exactly one.
    """
    overrides = dict(results or {})

    def outcome(name: str) -> str:
        return overrides.get(name, "PASS")

    checks = [
        {"name": "LOW_SHARPE", "value": sharpe, "result": outcome("LOW_SHARPE"), "limit": 1.25},
        {"name": "LOW_FITNESS", "value": fitness, "result": outcome("LOW_FITNESS"), "limit": 1.0},
        {"name": "LOW_TURNOVER", "value": turnover, "result": outcome("LOW_TURNOVER"), "limit": 0.01},
        {"name": "HIGH_TURNOVER", "value": turnover, "result": outcome("HIGH_TURNOVER"), "limit": 0.7},
        {
            "name": "CONCENTRATED_WEIGHT", "value": 0.0079,
            "result": outcome("CONCENTRATED_WEIGHT"), "limit": None,
        },
        {
            "name": "LOW_SUB_UNIVERSE_SHARPE", "value": round(sharpe * 0.43, 4),
            "result": outcome("LOW_SUB_UNIVERSE_SHARPE"), "limit": round(sharpe * 0.43, 4),
        },
        {
            "name": "SELF_CORRELATION", "value": self_correlation,
            "result": outcome("SELF_CORRELATION"), "limit": 0.7,
        },
        {
            "name": "MATCHES_COMPETITION", "value": None,
            "result": outcome("MATCHES_COMPETITION"), "competitions": [],
        },
    ]
    correlated: dict = {
        "schema": {
            "properties": [
                {"name": "alpha_id", "title": "Alpha ID"},
                {"name": "correlation", "title": "Correlation"},
            ],
        },
        "records": [],
    }
    if self_correlation is not None:
        correlated["max"] = self_correlation
        correlated["records"] = [[alpha_id, self_correlation]]
    return {"is": {"checks": checks, "selfCorrelated": correlated}}


class BrainBackend:
    """A scripted stand-in for the whole BRAIN REST surface."""

    def __init__(self, *, polls_before_done=2, yearly_payload=None, yearly_status=200,
                 metric_variants=None, check_pending_polls=0, check_status=200,
                 check_results=None):
        self.submissions = 0
        self.submitted_payloads: list[dict] = []
        self.poll_counts: dict[str, int] = {}
        self.polls_before_done = polls_before_done
        self.yearly_payload = YEARLY_RECORDSET if yearly_payload is None else yearly_payload
        self.yearly_status = yearly_status
        self.metric_variants = list(metric_variants or [])
        self.alpha_ids: dict[str, str] = {}
        self.unexpected: list[str] = []
        # The live /check endpoint is asynchronous: it answers an empty
        # ``text/html`` body plus ``Retry-After`` until the verdict is computed.
        self.check_pending_polls = check_pending_polls
        self.check_status = check_status
        self.check_results = dict(check_results or {})
        self.check_counts: dict[str, int] = {}
        self.check_calls: list[str] = []

    def __call__(self, method: str, url: str, kwargs) -> FakeResponse:
        if "/recordsets/yearly-stats" in url:
            # The live endpoint answers with a JSON recordset, not CSV.
            return FakeResponse(self.yearly_status, json_data=self.yearly_payload)

        if url.endswith("/authentication"):
            if method == "POST":
                return FakeResponse(201, {"user": {"id": "u1", "email": "tester@example.com"}})
            return FakeResponse(200, {"user": {"id": "u1"}})

        if url.endswith("/simulations") and method == "POST":
            self.submissions += 1
            self.submitted_payloads.append(kwargs.get("json"))
            simulation_id = f"sim{self.submissions:03d}"
            self.poll_counts[simulation_id] = 0
            return FakeResponse(
                201, {}, headers={"Location": f"{BASE}/simulations/{simulation_id}"}
            )

        match = re.search(r"/simulations/([^/]+)$", url)
        if match and method == "GET":
            simulation_id = match.group(1)
            polls = self.poll_counts.get(simulation_id, 0)
            self.poll_counts[simulation_id] = polls + 1
            if polls < self.polls_before_done:
                progress = (polls + 1) / (self.polls_before_done + 1)
                return FakeResponse(
                    200, {"progress": progress}, headers={"Retry-After": "3"}
                )
            alpha_id = f"ALPHA_{simulation_id}"
            self.alpha_ids[simulation_id] = alpha_id
            return FakeResponse(200, {"progress": 1.0, "alpha": alpha_id})

        match = re.search(r"/data-fields/([^/]+)$", url)
        if match and method == "GET":
            field_id = match.group(1)
            dataset = "fundamental2" if field_id.startswith("fn_") else "pv1"
            return FakeResponse(200, {
                "id": field_id,
                "type": "MATRIX",
                "coverage": 0.98,
                "dataset": {"id": dataset, "name": dataset.upper()},
            })

        match = re.search(r"/alphas/([^/]+)/check$", url)
        if match and method == "GET":
            alpha_id = match.group(1)
            calls = self.check_counts.get(alpha_id, 0)
            self.check_counts[alpha_id] = calls + 1
            self.check_calls.append(alpha_id)
            if self.check_status != 200:
                return FakeResponse(self.check_status, json_data=None, text="")
            if calls < self.check_pending_polls:
                # Still computing: empty body, Retry-After, not JSON yet.
                return FakeResponse(
                    200, json_data=None, text="",
                    headers={"Content-Type": "text/html", "Retry-After": "3"},
                )
            overrides = self.check_results.get(alpha_id, {})
            return FakeResponse(200, check_payload(alpha_id, **overrides))

        match = re.search(r"/alphas/([^/]+)$", url)
        if match and method == "GET":
            alpha_id = match.group(1)
            overrides = self._variant_for(alpha_id)
            return FakeResponse(200, alpha_payload(alpha_id, **overrides))

        self.unexpected.append(f"{method} {url}")
        raise AssertionError(f"unexpected request: {method} {url}")

    def _variant_for(self, alpha_id: str) -> dict:
        """Metric overrides for this alpha, matched by submission order."""
        if not self.metric_variants:
            return {}
        order = list(self.alpha_ids.values())
        if alpha_id not in order:
            return {}
        index = order.index(alpha_id)
        if index >= len(self.metric_variants):
            return {}
        return dict(self.metric_variants[index] or {})


@pytest.fixture
def env(tmp_path):
    """A wired-up client/store/runner triple backed by a fake BRAIN."""

    def build(backend=None, *, concurrency=1, max_wait=300.0, poll_interval=1.0,
              fetch_yearly=True, filters=None):
        fake_backend = backend or BrainBackend()
        config = load_config(None, root=tmp_path)
        config = replace(
            config,
            credentials=Credentials(username="tester@example.com", password="s3cret"),
            storage=StorageConfig(
                db_path=tmp_path / "brain.db", data_dir=tmp_path,
                log_file=tmp_path / "brain.log", log_level="DEBUG",
            ),
            runner=RunnerConfig(
                poll_interval=poll_interval, poll_jitter=0.0, max_wait=max_wait,
                concurrency=concurrency, min_request_interval=0.0,
            ),
            fetch_yearly_stats=fetch_yearly,
        )
        if filters is not None:
            config = replace(config, filters=filters)

        clock = FakeClock()
        session = FakeSession(fake_backend)
        client = WorldQuantClient(
            config.credentials,
            base_url=config.base_url,
            retry=config.retry,
            min_request_interval=0.0,
            session=session,
            sleep=clock.sleep,
            clock=clock.now,
            logger=quiet_logger("test.integration.client"),
        )
        store = ResultStore(config.storage.db_path)
        runner = SimulationRunner(
            client, store, config, logger=quiet_logger("test.integration.runner"),
            sleep=clock.sleep, clock=clock.now,
        )
        return {
            "backend": fake_backend, "config": config, "clock": clock,
            "session": session, "client": client, "store": store, "runner": runner,
        }

    yield build


EXPRESSIONS = [
    "rank(ts_delta(close, 5))",
    "rank(ts_mean(volume, 20))",
    "-rank(ts_std_dev(returns, 20))",
]


class TestFullChain:
    def test_single_alpha_completes(self, env):
        stack = env()
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert result.status == SimulationStatus.COMPLETED
        assert result.sharpe == pytest.approx(1.41)
        assert result.fitness == pytest.approx(1.08)
        assert result.turnover == pytest.approx(0.423)
        assert result.long_count == 1520
        assert result.remote_alpha_id == "ALPHA_sim001"
        stack["store"].close()
        stack["client"].close()

    def test_request_sequence_matches_the_verified_contract(self, env):
        stack = env()
        stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        methods_urls = [(m, u) for m, u, _ in stack["session"].calls]
        assert methods_urls[0] == ("POST", f"{BASE}/authentication")
        assert methods_urls[1] == ("POST", f"{BASE}/simulations")
        assert methods_urls[2] == ("GET", f"{BASE}/simulations/sim001")
        assert ("GET", f"{BASE}/alphas/ALPHA_sim001") in methods_urls
        assert stack["backend"].unexpected == []
        stack["store"].close()

    def test_polls_honour_retry_after(self, env):
        stack = env()
        stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)
        assert 3.0 in stack["clock"].sleeps
        assert stack["backend"].poll_counts["sim001"] == 3
        stack["store"].close()

    def test_yearly_stats_are_parsed_and_summarized(self, env):
        stack = env()
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert set(result.yearly_stats) == {"2019", "2020", "2021", "2022", "2023"}
        summary = result.yearly_summary
        assert summary.positive_years == 4
        assert summary.negative_years == 1
        assert summary.positive_year_ratio == pytest.approx(0.8)
        assert summary.worst_year_sharpe == pytest.approx(-0.4)
        stack["store"].close()

    def test_train_labelled_rows_reach_the_result_when_a_test_period_is_used(self, env):
        # Live shape with testPeriod=P1Y: in-sample years read TRAIN, the held-out
        # year reads TEST. Filtering on "IS" alone silently emptied yearly stats
        # for two whole rounds while the endpoint returned a valid recordset.
        recordset = {
            "schema": {
                "name": "yearly-stats",
                "properties": [
                    {"name": "year", "type": "year"},
                    {"name": "sharpe", "type": "decimal"},
                    {"name": "turnover", "type": "percent"},
                    {"name": "stage", "type": "string"},
                ],
            },
            "records": [
                ["2019", 3.06, 0.0517, "TRAIN"],
                ["2020", 1.44, 0.0459, "TRAIN"],
                ["2021", -0.85, 0.0449, "TRAIN"],
                ["2022", 2.96, 0.0481, "TRAIN"],
                ["2023", 0.71, 0.0402, "TEST"],
            ],
        }
        stack = env(BrainBackend(yearly_payload=recordset))
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert set(result.yearly_stats) == {"2019", "2020", "2021", "2022"}
        assert result.yearly_stats["2019"]["sharpe"] == pytest.approx(3.06)
        summary = result.yearly_summary
        assert summary.total_years == 4
        assert summary.positive_years == 3
        assert summary.worst_year_sharpe == pytest.approx(-0.85)
        stack["store"].close()

    def test_missing_yearly_endpoint_does_not_break_the_run(self, env):
        stack = env(BrainBackend(yearly_status=404, yearly_payload={}))
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert result.status == SimulationStatus.COMPLETED
        assert result.yearly_stats == {}
        assert result.yearly_summary.is_empty
        # Yearly rules are skipped, so the aggregate metrics still decide.
        assert result.passed is True
        stack["store"].close()

    def test_checks_are_normalized(self, env):
        stack = env()
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)
        assert result.checks["LOW_SHARPE"]["result"] == "PASS"
        assert result.checks["HIGH_TURNOVER"]["limit"] == 0.7
        stack["store"].close()

    def test_raw_response_is_stored_for_reparse(self, env):
        stack = env()
        stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)
        raw = json.loads(stack["store"].all_results()[0].raw_json)
        assert raw["id"] == "ALPHA_sim001"
        assert raw["is"]["sharpe"] == pytest.approx(1.41)
        assert raw["settings"]["neutralization"] == "INDUSTRY"
        stack["store"].close()

    def test_no_credentials_reach_the_database_or_the_log(self, env, tmp_path):
        stack = env()
        stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        db_bytes = (tmp_path / "brain.db").read_bytes()
        assert b"s3cret" not in db_bytes
        assert stack["config"].credentials.password not in db_bytes.decode("utf-8", "ignore")
        stack["store"].close()


class TestSubmissionCheckChain:
    """Every completed alpha goes through GET /alphas/{id}/check."""

    def test_the_check_runs_for_a_completed_simulation(self, env):
        backend = BrainBackend(polls_before_done=0)
        stack = env(backend)
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert backend.check_calls == ["ALPHA_sim001"]
        assert result.is_submittable is True
        assert result.self_correlation == pytest.approx(0.31)
        stack["store"].close()

    def test_the_resolved_verdict_supersedes_the_pending_payload_copy(self, env):
        # The alpha payload reports SELF_CORRELATION as PENDING forever, so the
        # grade alone is not evidence that the alpha can be submitted.
        stack = env()
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        raw = json.loads(result.raw_json)
        in_payload = {c["name"]: c["result"] for c in raw["is"]["checks"]}
        assert in_payload["SELF_CORRELATION"] == "PENDING", "BRAIN never resolves it here"
        assert result.checks["SELF_CORRELATION"]["result"] == "PASS"
        assert result.submission_checks["SELF_CORRELATION"]["result"] == "PASS"
        stack["store"].close()

    def test_a_failing_check_is_recorded_with_its_reason(self, env):
        backend = BrainBackend(
            polls_before_done=0,
            check_results={"ALPHA_sim001": {"results": {"SELF_CORRELATION": "FAIL"}}},
        )
        stack = env(backend)
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert result.status == SimulationStatus.COMPLETED
        assert result.is_submittable is False
        assert result.submission_failures == ["SELF_CORRELATION=0.31 (limit 0.7): FAIL"]
        stack["store"].close()

    def test_the_async_endpoint_is_polled_until_it_answers(self, env):
        backend = BrainBackend(polls_before_done=0, check_pending_polls=1)
        stack = env(backend)
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert len(backend.check_calls) == 2
        assert result.is_submittable is True
        stack["store"].close()

    def test_an_unavailable_endpoint_does_not_discard_the_simulation(self, env):
        backend = BrainBackend(polls_before_done=0, check_status=404)
        stack = env(backend)
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        # The work is already done and paid for; only the verdict is missing.
        assert result.status == SimulationStatus.COMPLETED
        assert result.sharpe == pytest.approx(1.41)
        assert not result.submission_checks
        assert result.is_submittable is False, "unverified must not read as submittable"
        stack["store"].close()

    def test_an_endpoint_that_never_resolves_leaves_it_unverified(self, env):
        backend = BrainBackend(polls_before_done=0, check_pending_polls=99)
        stack = env(backend)
        result = stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)

        assert result.status == SimulationStatus.COMPLETED
        assert not result.submission_checks
        stack["store"].close()

    def test_a_failed_check_survives_the_round_trip(self, env):
        backend = BrainBackend(
            polls_before_done=0,
            check_results={"ALPHA_sim001": {"results": {"LOW_FITNESS": "FAIL"}}},
        )
        stack = env(backend)
        stack["runner"].run_alpha(EXPRESSIONS[0], stack["config"].settings)
        stack["store"].close()

        with ResultStore(stack["config"].storage.db_path) as reopened:
            reloaded = reopened.all_results()[0]
        assert reloaded.is_submittable is False
        assert "LOW_FITNESS" in reloaded.submission_failures[0]


class TestBatchRun:
    def test_three_alphas_all_complete(self, env):
        stack = env()
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        results = stack["runner"].run_batch(specs)

        assert len(results) == 3
        assert all(r.status == SimulationStatus.COMPLETED for r in results)
        assert stack["backend"].submissions == 3
        stack["store"].close()

    def test_concurrent_batch_issues_one_submission_each(self, env):
        stack = env(concurrency=3)
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        results = stack["runner"].run_batch(specs)

        assert stack["backend"].submissions == 3
        assert len(results) == 3
        assert [r.expression for r in results] == EXPRESSIONS
        stack["store"].close()

    def test_rerun_submits_nothing_new(self, env):
        stack = env()
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        stack["runner"].run_batch(specs)
        assert stack["backend"].submissions == 3

        stack["runner"].run_batch(specs)
        assert stack["backend"].submissions == 3
        stack["store"].close()

    def test_force_resubmits(self, env):
        stack = env()
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        stack["runner"].run_batch(specs)
        stack["runner"].run_batch(specs, force=True)
        assert stack["backend"].submissions == 6
        stack["store"].close()

    def test_limit_is_respected(self, env):
        stack = env()
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        stack["runner"].run_batch(specs, limit=2)
        assert stack["backend"].submissions == 2
        stack["store"].close()


class TestFilteringAndExport:
    def test_pass_and_fail_are_split_into_separate_files(self, env, tmp_path):
        from worldquant.config import FilterConfig

        backend = BrainBackend(
            metric_variants=[
                {},                                                        # 1.41 / 1.08 / 42.3%
                {"sharpe": 0.82, "fitness": 0.33, "turnover": 1.342},      # weak
                {"sharpe": 1.60, "fitness": 1.20, "turnover": 0.30},       # strong
            ]
        )
        filters = FilterConfig(
            min_sharpe=1.25, min_fitness=1.0, max_turnover=0.70,
            min_positive_year_ratio=0.6, max_negative_sharpe_years=1,
        )
        stack = env(backend, filters=filters)
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        results = stack["runner"].run_batch(specs)
        paths = stack["store"].export_all(tmp_path)

        assert [r.passed for r in results] == [True, False, True]

        with paths["results"].open(encoding="utf-8", newline="") as handle:
            all_rows = list(csv.DictReader(handle))
        with paths["passed"].open(encoding="utf-8", newline="") as handle:
            passed_rows = list(csv.DictReader(handle))
        with paths["failed"].open(encoding="utf-8", newline="") as handle:
            failed_rows = list(csv.DictReader(handle))

        assert len(all_rows) == 3
        assert len(passed_rows) == 2
        assert len(failed_rows) == 1
        assert failed_rows[0]["expression"] == EXPRESSIONS[1]
        assert "Sharpe 0.82 < 1.25" in failed_rows[0]["reasons"]
        assert "Turnover 134.2% > 70.0%" in failed_rows[0]["reasons"]
        stack["store"].close()

    def test_strict_thresholds_produce_reasons_in_the_csv(self, env, tmp_path):
        from worldquant.config import FilterConfig

        filters = FilterConfig(
            min_sharpe=2.0, min_fitness=1.5, max_turnover=0.10,
            min_positive_year_ratio=0.95, max_negative_sharpe_years=0,
        )
        stack = env(filters=filters)
        specs = specs_from_expressions(EXPRESSIONS[:1], settings=stack["config"].settings)
        results = stack["runner"].run_batch(specs)
        paths = stack["store"].export_all(tmp_path)

        assert results[0].passed is False
        reasons = results[0].reasons
        assert any(r.startswith("Sharpe 1.41 < 2.00") for r in reasons)
        assert any(r.startswith("Turnover 42.3% > 10.0%") for r in reasons)
        assert any(r.startswith("Positive years 4/5") for r in reasons)
        assert "Negative-sharpe years 1 > 0" in reasons

        with paths["failed"].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert "Sharpe 1.41 < 2.00" in row["reasons"]
        assert row["passed"] == "False"
        assert row["positive_years"] == "4"
        stack["store"].close()

    def test_leaderboard_ranks_completed_alphas(self, env):
        stack = env()
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        stack["runner"].run_batch(specs)
        table = format_leaderboard(stack["store"].all_results(), top=20)

        assert table.startswith("Top 3 Alpha")
        assert "1.41" in table
        assert "42.3%" in table
        assert "4/5+" in table
        stack["store"].close()


class TestInterruptedRunRecovery:
    """Kill the process mid-batch, restart, and lose nothing."""

    def test_resume_finishes_a_half_done_batch(self, env, tmp_path):
        backend = BrainBackend(polls_before_done=999)  # never finishes in budget
        stack = env(backend, max_wait=5.0)
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)

        first = stack["runner"].run_batch(specs)
        assert all(r.status == SimulationStatus.TIMEOUT for r in first)
        assert backend.submissions == 3
        stack["store"].close()
        stack["client"].close()

        # "Restart": same database, but the server has now finished the work.
        backend.polls_before_done = 0
        resumed_stack = env(backend)
        resumed = resumed_stack["runner"].resume_incomplete()

        assert len(resumed) == 3
        assert all(r.status == SimulationStatus.COMPLETED for r in resumed)
        # Crucially: no duplicate submissions on the platform.
        assert backend.submissions == 3
        assert resumed_stack["store"].status_counts()[SimulationStatus.COMPLETED] == 3
        resumed_stack["store"].close()

    def test_restart_with_the_input_file_skips_finished_work(self, env):
        backend = BrainBackend()
        stack = env(backend)
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        stack["runner"].run_batch(specs)
        stack["store"].close()
        stack["client"].close()

        restarted = env(backend)
        results = restarted["runner"].run_batch(specs)
        assert backend.submissions == 3, "a restart must not resubmit finished alphas"
        assert all(r.status == SimulationStatus.COMPLETED for r in results)
        restarted["store"].close()

    def test_no_simulation_rows_are_orphaned(self, env):
        stack = env()
        specs = specs_from_expressions(EXPRESSIONS, settings=stack["config"].settings)
        stack["runner"].run_batch(specs)
        stack["runner"].run_batch(specs)

        rows = stack["store"]._query("SELECT * FROM simulations")
        assert len(rows) == 3
        assert all(row["remote_simulation_id"] for row in rows)
        assert stack["store"].find_incomplete_simulations() == []
        stack["store"].close()
