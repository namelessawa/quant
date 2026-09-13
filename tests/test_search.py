"""Grade-targeted search script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import FakeClock, FakeSession
from conftest import submission_checks as all_pass_checks
from test_integration import BrainBackend
from worldquant.api import AlphaGrade, SimulationStatus
from worldquant.client import WorldQuantClient
from worldquant.exceptions import ConfigError
from worldquant.storage import ResultStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import search_alpha  # noqa: E402


@pytest.fixture
def search(tmp_path, monkeypatch):
    """Wire search_alpha to a fake BRAIN backend and isolated storage."""
    monkeypatch.setattr("worldquant.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("WQBRAIN_USERNAME", "tester@example.com")
    monkeypatch.setenv("WQBRAIN_PASSWORD", "s3cret")
    clock = FakeClock()
    state: dict = {}

    def install(backend):
        def factory(credentials, **kwargs):
            kwargs.pop("min_request_interval", None)
            return WorldQuantClient(
                credentials, session=FakeSession(backend), sleep=clock.sleep,
                clock=clock.now, min_request_interval=0.0, **kwargs,
            )

        monkeypatch.setattr(search_alpha, "WorldQuantClient", factory)
        state["backend"] = backend

    common = [
        "--db", str(tmp_path / "search.db"),
        "--log-file", str(tmp_path / "search.log"),
        "--concurrency", "1",
        "--poll-interval", "1",
        "--max-wait", "300",
        "--no-yearly",
    ]

    def run(*args, backend=None):
        install(backend or BrainBackend(polls_before_done=0))
        return search_alpha.main([*args, *common])

    return {"run": run, "install": install, "state": state, "tmp_path": tmp_path,
            "clock": clock}


def grades_in_order(*grades):
    """A backend handing out one grade per submission, in order."""
    return BrainBackend(
        polls_before_done=0,
        metric_variants=[{"grade": grade} for grade in grades],
    )


def grades_with_checks(*entries):
    """A backend handing out ``(grade, check_overrides)`` per submission.

    ``check_overrides`` is passed straight to :func:`test_integration.check_payload`,
    so ``{"results": {"SELF_CORRELATION": "FAIL"}}`` makes exactly one of the
    eight checks fail while the other seven still PASS. ``None`` leaves the
    default all-PASS verdict in place.
    """
    check_results = {}
    for index, (_, overrides) in enumerate(entries, start=1):
        if overrides:
            check_results[f"ALPHA_sim{index:03d}"] = overrides
    return BrainBackend(
        polls_before_done=0,
        metric_variants=[{"grade": grade} for grade, _ in entries],
        check_results=check_results,
    )


class TestPoolConstruction:
    def test_seeds_come_first(self):
        pool = search_alpha.build_pool()
        assert pool[: len(search_alpha.SEED_EXPRESSIONS)] == list(search_alpha.SEED_EXPRESSIONS)

    def test_seeds_are_the_analyst_yield_family(self):
        seeds = list(search_alpha.SEED_EXPRESSIONS)
        # Blends first, then one single-field stack and one plain yield per numerator.
        expected = len(search_alpha.CONSENSUS_FIELDS) + 2 * len(search_alpha.YIELD_NUMERATORS)
        assert len(seeds) == expected
        for numerator, denominator in search_alpha.YIELD_NUMERATORS:
            assert any(numerator in s and f"/ {denominator}" in s for s in seeds)

    def test_blends_precede_single_field_stacks_which_precede_plain_yields(self):
        seeds = list(search_alpha.SEED_EXPRESSIONS)
        blends = len(search_alpha.CONSENSUS_FIELDS)
        # A blend references both an aggregate yield and a consensus field.
        assert all("anl4_ebit_value" in s for s in seeds[:blends])
        # trade_when was dropped: ablation showed it cost more signal (sharpe
        # 1.32) than the volatility it avoided (1.41 without it).
        assert all("trade_when" not in s for s in seeds)
        # The plain ranks come last.
        assert all(s.startswith("rank(") for s in seeds[-len(search_alpha.YIELD_NUMERATORS):])

    def test_seeds_group_by_subindustry_not_industry(self):
        # subindustry beat industry on the same field: fitness 0.82 -> 1.00.
        for seed in search_alpha.SEED_EXPRESSIONS:
            if "group_zscore" in seed:
                assert ", subindustry)" in seed
                assert ", industry)" not in seed

    def test_pool_operators_are_ones_the_account_can_access(self):
        # GET /operators returns 66 accessible operators and ts_max / ts_min are
        # not among them; using one fails the simulation outright rather than
        # scoring badly, which silently wasted three round-24 candidates.
        inaccessible = {"ts_max", "ts_min"}
        assert not inaccessible & set(search_alpha.POOL_OPERATORS)
        assert "ts_zscore" in search_alpha.POOL_OPERATORS
        assert "ts_arg_max" in search_alpha.POOL_OPERATORS

    def test_price_volume_cross_product_comes_last(self):
        pool = search_alpha.build_pool()
        tail = pool[len(search_alpha.SEED_EXPRESSIONS):]
        assert tail, "expected a price/volume tail"
        assert all("trade_when" not in expression for expression in tail)
        assert all(expression not in search_alpha.SEED_EXPRESSIONS for expression in tail)

    def test_negated_variants_precede_positive_ones(self):
        pool = search_alpha.build_pool()
        first_negated = next(i for i, e in enumerate(pool) if e.startswith("-rank(ts_delta(close"))
        first_positive = next(
            i for i, e in enumerate(pool)
            if e.startswith("rank(ts_delta(close") and not e.startswith("-")
        )
        assert first_negated < first_positive

    def test_pool_has_no_duplicates(self):
        pool = search_alpha.build_pool()
        assert len(pool) == len(set(pool))

    def test_pool_covers_the_full_cross_product(self):
        pool = search_alpha.build_pool()
        expected = (
            len(search_alpha.SEED_EXPRESSIONS)
            + 2 * len(search_alpha.POOL_OPERATORS)
            * len(search_alpha.POOL_FIELDS)
            * len(search_alpha.POOL_WINDOWS)
        )
        # Seeds may overlap the cross product, so the pool is at most this large.
        assert len(pool) <= expected
        assert len(pool) > 2 * len(search_alpha.POOL_FIELDS) * len(search_alpha.POOL_WINDOWS)

    def test_positive_first_ordering_is_available(self):
        pool = search_alpha.build_pool(negate_first=False)
        seeds = list(search_alpha.SEED_EXPRESSIONS)
        rest = pool[len(seeds):]
        assert rest[0].startswith("rank(")


class TestSearchStopsOnTarget:
    def test_stops_at_the_first_matching_grade(self, search):
        backend = grades_in_order("INFERIOR", "INFERIOR", "AVERAGE", "GOOD")
        code = search["run"]("--target-grade", "AVERAGE", "--max-attempts", "10", backend=backend)

        assert code == search_alpha.EXIT_FOUND
        assert backend.submissions == 3, "must stop as soon as the target grade appears"

    def test_does_not_submit_the_rest_of_the_pool(self, search):
        backend = grades_in_order("AVERAGE")
        code = search["run"]("--target-grade", "AVERAGE", "--max-attempts", "50", backend=backend)

        assert code == search_alpha.EXIT_FOUND
        assert backend.submissions == 1

    def test_hits_on_the_first_candidate(self, search):
        backend = grades_in_order("AVERAGE")
        assert search["run"]("--target-grade", "AVERAGE", backend=backend) == 0
        assert backend.submissions == 1

    def test_target_grade_is_case_insensitive(self, search):
        backend = grades_in_order("AVERAGE")
        assert search["run"]("--target-grade", "average", backend=backend) == 0

    def test_searching_for_a_different_grade(self, search):
        backend = grades_in_order("INFERIOR", "GOOD")
        assert search["run"]("--target-grade", "GOOD", "--max-attempts", "5",
                             backend=backend) == search_alpha.EXIT_FOUND
        assert backend.submissions == 2

    def test_grade_is_persisted_alongside_the_hit(self, search):
        backend = grades_in_order("INFERIOR", "AVERAGE")
        search["run"]("--target-grade", "AVERAGE", backend=backend)

        with ResultStore(search["tmp_path"] / "search.db") as store:
            found = store.find_by_grade("AVERAGE")
            assert len(found) == 1
            assert found[0].status == SimulationStatus.COMPLETED
            assert store.grade_counts() == {"INFERIOR": 1, "AVERAGE": 1}


class TestSearchExhaustion:
    def test_no_match_returns_not_found(self, search):
        backend = grades_in_order("INFERIOR", "INFERIOR", "INFERIOR")
        code = search["run"]("--target-grade", "AVERAGE", "--max-attempts", "3", backend=backend)
        assert code == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 3

    def test_max_attempts_caps_the_submissions(self, search):
        backend = BrainBackend(polls_before_done=0)
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "4", backend=backend)
        assert code == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 4

    def test_zero_attempts_is_a_usage_error(self, search):
        backend = BrainBackend(polls_before_done=0)
        code = search["run"]("--target-grade", "AVERAGE", "--max-attempts", "0", backend=backend)
        assert code == search_alpha.EXIT_USAGE_ERROR
        assert backend.submissions == 0


class TestExistingResults:
    def test_include_existing_short_circuits_without_network(self, search):
        # Seed a stored AVERAGE result, then search with a backend that would
        # fail the test if it were ever touched.
        db_path = search["tmp_path"] / "search.db"
        settings = {"region": "USA", "delay": 1}
        with ResultStore(db_path) as store:
            alpha_row = store.upsert_alpha("rank(stored)", settings, name="stored")
            sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim_old")
            store.update_simulation(
                sim_row, status=SimulationStatus.COMPLETED, remote_alpha_id="OLD1",
                mark_completed=True,
            )
            from worldquant.models import AlphaResult
            from worldquant.hashing import dedup_key

            store.save_result(sim_row, AlphaResult(
                alpha_id="stored", expression="rank(stored)",
                dedup_key=dedup_key("rank(stored)", settings),
                status=SimulationStatus.COMPLETED, remote_alpha_id="OLD1",
                sharpe=1.5, grade=AlphaGrade.AVERAGE, stage="IS",
                # The gate needs a recorded verdict: an unchecked stored alpha is
                # no longer reported as a hit.
                submission_checks=all_pass_checks(), self_correlation=0.31,
            ))

        class ExplodingBackend:
            def __call__(self, method, url, kwargs):
                raise AssertionError("must not touch the network when a stored hit exists")

        search["install"](ExplodingBackend())
        code = search_alpha.main([
            "--target-grade", "AVERAGE", "--include-existing",
            "--db", str(db_path), "--log-file", str(search["tmp_path"] / "s.log"),
            "--concurrency", "1", "--no-yearly",
        ])
        assert code == search_alpha.EXIT_FOUND

    def test_without_include_existing_it_keeps_searching(self, search):
        db_path = search["tmp_path"] / "search.db"
        settings = {"region": "USA", "delay": 1}
        with ResultStore(db_path) as store:
            alpha_row = store.upsert_alpha("rank(stored)", settings, name="stored")
            sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim_old")
            store.update_simulation(
                sim_row, status=SimulationStatus.COMPLETED, remote_alpha_id="OLD1",
                mark_completed=True,
            )
            from worldquant.models import AlphaResult
            from worldquant.hashing import dedup_key

            store.save_result(sim_row, AlphaResult(
                alpha_id="stored", expression="rank(stored)",
                dedup_key=dedup_key("rank(stored)", settings),
                status=SimulationStatus.COMPLETED, remote_alpha_id="OLD1",
                grade=AlphaGrade.AVERAGE,
            ))

        backend = grades_in_order("INFERIOR")
        search["install"](backend)
        code = search_alpha.main([
            "--target-grade", "AVERAGE", "--max-attempts", "1",
            "--db", str(db_path), "--log-file", str(search["tmp_path"] / "s.log"),
            "--concurrency", "1", "--no-yearly",
        ])
        # The stored hit is ignored, so the single new attempt decides the outcome.
        assert code == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 1


class TestGradeMeetsTarget:
    """The target is a floor, not an exact match."""

    def test_exact_match_satisfies(self):
        assert search_alpha.grade_meets_target("GOOD", "GOOD") is True

    def test_better_grade_satisfies(self):
        # A live run hunting GOOD once produced EXCELLENT and walked straight
        # past it because the comparison was `grade == target`.
        assert search_alpha.grade_meets_target("EXCELLENT", "GOOD") is True
        assert search_alpha.grade_meets_target("GOOD", "AVERAGE") is True
        assert search_alpha.grade_meets_target("EXCELLENT", "INFERIOR") is True

    def test_worse_grade_does_not_satisfy(self):
        assert search_alpha.grade_meets_target("AVERAGE", "GOOD") is False
        assert search_alpha.grade_meets_target("INFERIOR", "AVERAGE") is False
        assert search_alpha.grade_meets_target("GOOD", "EXCELLENT") is False

    def test_missing_grade_does_not_satisfy(self):
        assert search_alpha.grade_meets_target(None, "GOOD") is False
        assert search_alpha.grade_meets_target("", "GOOD") is False

    def test_unrecognized_target_matches_nothing(self):
        # A typo must not silently accept every result.
        assert search_alpha.grade_meets_target("EXCELLENT", "GOD") is False
        assert search_alpha.grade_meets_target("GOOD", "") is False

    def test_unrecognized_grade_does_not_satisfy(self):
        assert search_alpha.grade_meets_target("SOMETHING_NEW", "GOOD") is False

    def test_search_for_good_stops_on_excellent(self, search):
        backend = grades_in_order("INFERIOR", "EXCELLENT", "GOOD")
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "9", backend=backend)
        assert code == search_alpha.EXIT_FOUND
        assert backend.submissions == 2, "must stop at the EXCELLENT result"

    def test_best_fitness_wins_within_the_top_grade(self, search, caplog):
        # Several alphas can share the top grade; the strongest is the useful
        # answer, not whichever the database happened to return first.
        from worldquant.hashing import dedup_key
        from worldquant.models import AlphaResult

        db_path = search["tmp_path"] / "search.db"
        settings = {"region": "USA", "delay": 1}
        with ResultStore(db_path) as store:
            for name, expression, fitness, alpha_id in (
                ("weaker", "rank(weak / cap)", 2.07, "WEAK1"),
                ("stronger", "rank(strong / cap)", 2.30, "STRON1"),
            ):
                alpha_row = store.upsert_alpha(expression, settings, name=name)
                sim_row = store.create_simulation(alpha_row, remote_simulation_id=f"sim_{name}")
                store.update_simulation(
                    sim_row, status=SimulationStatus.COMPLETED,
                    remote_alpha_id=alpha_id, mark_completed=True,
                )
                store.save_result(sim_row, AlphaResult(
                    alpha_id=name, expression=expression,
                    dedup_key=dedup_key(expression, settings),
                    status=SimulationStatus.COMPLETED, remote_alpha_id=alpha_id,
                    sharpe=2.2, fitness=fitness, grade="EXCELLENT",
                    long_count=1500, short_count=1470,
                    submission_checks=all_pass_checks(), self_correlation=0.31,
                ))

        class Exploding:
            def __call__(self, method, url, kwargs):
                raise AssertionError("must not touch the network for a stored hit")

        search["install"](Exploding())
        assert search_alpha.main([
            "--target-grade", "GOOD", "--include-existing",
            "--db", str(db_path), "--log-file", str(search["tmp_path"] / "s.log"),
            "--concurrency", "1", "--no-yearly",
        ]) == search_alpha.EXIT_FOUND

        logged = caplog.text + (search["tmp_path"] / "s.log").read_text(encoding="utf-8")
        assert "STRON1" in logged
        assert "stronger" in logged or "rank(strong / cap)" in logged


class TestOneSidedBook:
    """A long-only book can grade GOOD on concentration alone; it must be flagged."""

    def make(self, **overrides):
        from worldquant.models import AlphaResult

        base = dict(
            alpha_id="a1", expression="rank(x / cap)", dedup_key="k",
            status=SimulationStatus.COMPLETED, grade="GOOD",
            sharpe=0.91, fitness=1.66, turnover=0.0104, returns=0.416,
            long_count=2803, short_count=0, remote_alpha_id="om6zPKjn",
        )
        base.update(overrides)
        return AlphaResult(**base)

    def test_long_only_is_one_sided(self):
        assert self.make(long_count=2803, short_count=0).is_one_sided_book is True

    def test_short_only_is_one_sided(self):
        assert self.make(long_count=0, short_count=1400).is_one_sided_book is True

    def test_balanced_book_is_not_flagged(self):
        assert self.make(long_count=1443, short_count=1389).is_one_sided_book is False

    def test_unknown_counts_are_not_flagged(self):
        # An incomplete run must not be reported as degenerate.
        assert self.make(long_count=None, short_count=None).is_one_sided_book is False
        assert self.make(long_count=100, short_count=None).is_one_sided_book is False

    def test_report_hit_warns_about_a_one_sided_book(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.oneSided")
        log.propagate = True
        with caplog.at_level("WARNING", logger="test.report.oneSided"):
            search_alpha.report_hit(self.make(), log, attempts=1)
        assert "one-sided book" in caplog.text
        assert "long=2803" in caplog.text

    def test_report_hit_stays_silent_for_a_balanced_book(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.balanced")
        log.propagate = True
        with caplog.at_level("WARNING", logger="test.report.balanced"):
            search_alpha.report_hit(
                self.make(long_count=1443, short_count=1389), log, attempts=1
            )
        assert "one-sided" not in caplog.text

    def test_report_hit_shows_the_book_shape(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.shape")
        log.propagate = True
        with caplog.at_level("INFO", logger="test.report.shape"):
            search_alpha.report_hit(
                self.make(long_count=1443, short_count=1389), log, attempts=1
            )
        assert "long/short : 1443 / 1389" in caplog.text


class TestOverfitRatio:
    """The mandatory test period exists to expose overfitting; make it visible."""

    def make(self, **overrides):
        from worldquant.models import AlphaResult

        base = dict(
            alpha_id="a1", expression="rank(x / cap)", dedup_key="k",
            status=SimulationStatus.COMPLETED, grade="GOOD",
            sharpe=1.80, fitness=1.71, long_count=1121, short_count=1118,
            remote_alpha_id="kqVL1PLd",
        )
        base.update(overrides)
        return AlphaResult(**base)

    def test_reads_the_held_out_year(self):
        assert self.make(test_stats={"sharpe": 0.46, "fitness": 0.19}).test_sharpe == pytest.approx(0.46)

    def test_no_test_period_gives_none(self):
        assert self.make().test_sharpe is None
        assert self.make().overfit_ratio is None

    def test_ratio_matches_the_live_example(self):
        # IS sharpe 1.80 graded GOOD while the held-out year returned 0.46.
        result = self.make(sharpe=1.80, test_stats={"sharpe": 0.46})
        assert result.overfit_ratio == pytest.approx(0.46 / 1.80)
        assert result.overfit_ratio < 0.5, "should trip the overfitting warning"

    def test_healthy_alpha_has_a_high_ratio(self):
        result = self.make(sharpe=1.80, test_stats={"sharpe": 1.60})
        assert result.overfit_ratio == pytest.approx(1.60 / 1.80)
        assert result.overfit_ratio >= 0.5

    def test_ratio_is_none_for_a_non_positive_is_sharpe(self):
        # A ratio against a negative baseline is meaningless, not "very overfit".
        assert self.make(sharpe=-1.20, test_stats={"sharpe": 0.4}).overfit_ratio is None
        assert self.make(sharpe=0.0, test_stats={"sharpe": 0.4}).overfit_ratio is None

    def test_ratio_is_none_when_the_test_sharpe_is_missing(self):
        assert self.make(sharpe=1.8, test_stats={"fitness": 0.2}).overfit_ratio is None
        assert self.make(sharpe=1.8, test_stats={}).overfit_ratio is None

    def test_metrics_line_shows_the_test_sharpe_when_present(self):
        line = self.make(test_stats={"sharpe": 0.46}).metrics_line()
        assert "TestSharpe=0.46" in line
        assert "Grade=GOOD" in line

    def test_metrics_line_omits_it_without_a_test_period(self):
        assert "TestSharpe" not in self.make().metrics_line()

    def test_report_hit_surfaces_the_test_sharpe(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.overfit")
        log.propagate = True
        with caplog.at_level("INFO", logger="test.report.overfit"):
            search_alpha.report_hit(self.make(test_stats={"sharpe": 0.46}), log, attempts=1)
        assert "TestSharpe=0.46" in caplog.text


class TestSubmissionGate:
    """Grade is only half the rule: all eight submission checks must PASS too."""

    def make(self, **overrides):
        from worldquant.models import AlphaResult

        base = dict(
            alpha_id="a1", expression="rank(x / cap)", dedup_key="k",
            status=SimulationStatus.COMPLETED, grade="GOOD",
            sharpe=2.28, fitness=2.69, turnover=0.0478, returns=0.1745,
            long_count=1467, short_count=1414, remote_alpha_id="2rOn70lb",
            submission_checks=all_pass_checks(), self_correlation=0.6996,
        )
        base.update(overrides)
        return AlphaResult(**base)

    # -- is_acceptable_hit --------------------------------------------------
    def test_grade_plus_eight_passes_is_a_hit(self):
        assert search_alpha.is_acceptable_hit(self.make(), "GOOD") is True

    def test_a_better_grade_is_still_a_hit(self):
        assert search_alpha.is_acceptable_hit(self.make(grade="EXCELLENT"), "GOOD") is True

    def test_a_failing_check_vetoes_a_good_grade(self):
        checks = all_pass_checks({"SELF_CORRELATION": "FAIL"})
        result = self.make(submission_checks=checks)
        assert result.grade == "GOOD"
        assert search_alpha.is_acceptable_hit(result, "GOOD") is False

    def test_a_pending_check_vetoes_a_good_grade(self):
        checks = all_pass_checks({"SELF_CORRELATION": "PENDING"})
        assert search_alpha.is_acceptable_hit(self.make(submission_checks=checks), "GOOD") is False

    def test_an_unchecked_alpha_is_not_a_hit(self):
        # None means the check never ran, which is not the same as passing it.
        for checks in (None, {}):
            assert search_alpha.is_acceptable_hit(
                self.make(submission_checks=checks), "GOOD"
            ) is False

    def test_a_missing_check_vetoes_even_with_seven_passes(self):
        checks = all_pass_checks()
        checks.pop("MATCHES_COMPETITION")
        assert search_alpha.is_acceptable_hit(self.make(submission_checks=checks), "GOOD") is False

    def test_a_worse_grade_is_not_a_hit_even_when_submittable(self):
        assert search_alpha.is_acceptable_hit(self.make(grade="AVERAGE"), "GOOD") is False

    # -- describe_submission ------------------------------------------------
    def test_verdict_quotes_the_eight_checks(self):
        verdict = search_alpha.describe_submission(self.make())
        assert "8/8 checks PASS" in verdict
        assert "self-correlation=0.6996" in verdict
        assert "FAILING" not in verdict

    def test_verdict_names_what_is_blocking(self):
        checks = all_pass_checks({"LOW_FITNESS": "FAIL"})
        verdict = search_alpha.describe_submission(self.make(submission_checks=checks))
        assert "7/8 checks PASS" in verdict
        assert "FAILING: LOW_FITNESS" in verdict

    def test_unverified_reads_differently_from_failed(self):
        unverified = search_alpha.describe_submission(self.make(submission_checks=None))
        failed = search_alpha.describe_submission(
            self.make(submission_checks=all_pass_checks({"LOW_FITNESS": "FAIL"}))
        )
        assert "UNVERIFIED" in unverified
        assert "FAILING" not in unverified
        assert "UNVERIFIED" not in failed

    def test_verdict_omits_correlation_when_unresolved(self):
        assert "self-correlation" not in search_alpha.describe_submission(
            self.make(self_correlation=None)
        )

    # -- _stage_line --------------------------------------------------------
    def test_stage_line_renders_ratios_as_percents(self):
        line = search_alpha._stage_line(
            {"sharpe": 0.71, "fitness": 0.22, "returns": 0.031, "turnover": 0.05, "drawdown": 0.02}
        )
        assert line == "sharpe=0.71 fitness=0.22 returns=3.1% turnover=5.0% drawdown=2.0%"

    def test_stage_line_skips_missing_and_non_numeric_values(self):
        assert search_alpha._stage_line({"sharpe": 1.2, "fitness": None, "returns": "n/a"}) == "sharpe=1.20"

    @pytest.mark.parametrize("stage", [None, {}, "nope"])
    def test_stage_line_without_a_test_period(self, stage):
        assert search_alpha._stage_line(stage) == "n/a"

    # -- report_hit ---------------------------------------------------------
    def test_report_hit_shows_the_submission_verdict(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.gate")
        log.propagate = True
        with caplog.at_level("INFO", logger="test.report.gate"):
            search_alpha.report_hit(self.make(), log, attempts=1)
        assert "submission : 8/8 checks PASS" in caplog.text

    def test_report_hit_shows_the_held_out_year(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.stages")
        log.propagate = True
        result = self.make(
            train_stats={"sharpe": 2.62, "fitness": 3.1, "returns": 0.21},
            test_stats={"sharpe": 0.71, "fitness": 0.22, "returns": 0.031},
        )
        with caplog.at_level("INFO", logger="test.report.stages"):
            search_alpha.report_hit(result, log, attempts=1)
        assert "train      : sharpe=2.62 fitness=3.10 returns=21.0%" in caplog.text
        assert "test (P1Y) : sharpe=0.71 fitness=0.22 returns=3.1%" in caplog.text

    def test_report_hit_omits_the_split_without_a_test_period(self, caplog):
        from conftest import quiet_logger

        log = quiet_logger("test.report.nostages")
        log.propagate = True
        with caplog.at_level("INFO", logger="test.report.nostages"):
            search_alpha.report_hit(self.make(), log, attempts=1)
        assert "test (P1Y)" not in caplog.text

    # -- end to end ---------------------------------------------------------
    def test_a_good_grade_with_a_failing_check_is_not_found(self, search, caplog):
        backend = grades_with_checks(("GOOD", {"results": {"SELF_CORRELATION": "FAIL"}}))
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        assert code == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 1
        assert backend.check_calls == ["ALPHA_sim001"], "the check must actually run"

    def test_the_rejection_names_the_blocking_check(self, search):
        backend = grades_with_checks(("GOOD", {"results": {"SELF_CORRELATION": "FAIL"}}))
        search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        logged = (search["tmp_path"] / "search.log").read_text(encoding="utf-8")
        assert "NOT submittable" in logged
        assert "SELF_CORRELATION" in logged

    def test_the_search_moves_on_to_a_submittable_candidate(self, search):
        # Abandoning the rejected alpha and trying the next one is the point of
        # the loop: the EXCELLENT result is unusable, the GOOD one is not.
        backend = grades_with_checks(
            ("EXCELLENT", {"results": {"HIGH_TURNOVER": "FAIL"}}),
            ("GOOD", None),
        )
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "5", backend=backend)
        assert code == search_alpha.EXIT_FOUND
        assert backend.submissions == 2
        assert backend.check_calls == ["ALPHA_sim001", "ALPHA_sim002"]

    def test_a_pending_check_is_not_found(self, search):
        backend = grades_with_checks(("GOOD", {"results": {"SELF_CORRELATION": "PENDING"}}))
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        assert code == search_alpha.EXIT_NOT_FOUND

    def test_an_unavailable_check_endpoint_leaves_it_unverified(self, search):
        # A completed simulation must survive an unavailable /check endpoint, but
        # the alpha cannot be reported as a find.
        backend = BrainBackend(
            polls_before_done=0, metric_variants=[{"grade": "GOOD"}], check_status=404,
        )
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        assert code == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 1

    def test_an_always_pending_check_endpoint_leaves_it_unverified(self, search):
        backend = BrainBackend(
            polls_before_done=0, metric_variants=[{"grade": "GOOD"}],
            check_pending_polls=99,
        )
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        assert code == search_alpha.EXIT_NOT_FOUND
        assert len(backend.check_calls) == 3, "the client retries a bounded number of times"

    def test_the_async_endpoint_is_polled_until_it_answers(self, search):
        backend = BrainBackend(
            polls_before_done=0, metric_variants=[{"grade": "GOOD"}], check_pending_polls=1,
        )
        code = search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        assert code == search_alpha.EXIT_FOUND
        assert len(backend.check_calls) == 2

    def test_the_hit_is_stored_as_submittable(self, search):
        backend = grades_with_checks(("GOOD", None))
        search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        with ResultStore(search["tmp_path"] / "search.db") as store:
            stored = store.find_by_grade("GOOD")
        assert len(stored) == 1
        assert stored[0].is_submittable is True
        assert stored[0].self_correlation == pytest.approx(0.31)

    def test_a_rejected_alpha_is_still_stored_with_its_verdict(self, search):
        # The ledger has to show why it was abandoned, not just that it was.
        backend = grades_with_checks(("GOOD", {"results": {"LOW_FITNESS": "FAIL"}}))
        search["run"]("--target-grade", "GOOD", "--max-attempts", "1", backend=backend)
        with ResultStore(search["tmp_path"] / "search.db") as store:
            stored = store.find_by_grade("GOOD")
        assert len(stored) == 1
        assert stored[0].is_submittable is False
        assert stored[0].submission_failures == ["LOW_FITNESS=1.08 (limit 1.0): FAIL"]


class TestIncludeExistingGate:
    """--include-existing must not report a stored alpha nobody ever checked."""

    def seed(self, db_path, *, checks, grade="GOOD", alpha_id="OLD1"):
        from worldquant.hashing import dedup_key
        from worldquant.models import AlphaResult

        settings = {"region": "USA", "delay": 1}
        with ResultStore(db_path) as store:
            alpha_row = store.upsert_alpha("rank(stored)", settings, name="stored")
            sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim_old")
            store.update_simulation(
                sim_row, status=SimulationStatus.COMPLETED,
                remote_alpha_id=alpha_id, mark_completed=True,
            )
            store.save_result(sim_row, AlphaResult(
                alpha_id="stored", expression="rank(stored)",
                dedup_key=dedup_key("rank(stored)", settings),
                status=SimulationStatus.COMPLETED, remote_alpha_id=alpha_id,
                sharpe=2.2, fitness=1.6, grade=grade,
                submission_checks=checks,
                self_correlation=None if checks is None else 0.31,
            ))

    def run(self, search, db_path, backend):
        search["install"](backend)
        return search_alpha.main([
            "--target-grade", "GOOD", "--include-existing",
            "--db", str(db_path), "--log-file", str(search["tmp_path"] / "s.log"),
            "--concurrency", "1", "--no-yearly", "--max-attempts", "1",
        ])

    def test_a_verified_stored_hit_short_circuits(self, search):
        class Exploding:
            def __call__(self, method, url, kwargs):
                raise AssertionError("must not touch the network for a stored hit")

        db_path = search["tmp_path"] / "search.db"
        self.seed(db_path, checks=all_pass_checks())
        assert self.run(search, db_path, Exploding()) == search_alpha.EXIT_FOUND

    def test_an_unchecked_stored_alpha_is_not_reported(self, search):
        db_path = search["tmp_path"] / "search.db"
        self.seed(db_path, checks=None)
        backend = grades_in_order("INFERIOR")
        assert self.run(search, db_path, backend) == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 1, "the search must fall through to new candidates"

    def test_a_stored_alpha_with_a_failing_check_is_not_reported(self, search):
        db_path = search["tmp_path"] / "search.db"
        self.seed(db_path, checks=all_pass_checks({"SELF_CORRELATION": "FAIL"}))
        backend = grades_in_order("INFERIOR")
        assert self.run(search, db_path, backend) == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 1

    def test_the_skip_is_logged_with_its_reason(self, search):
        db_path = search["tmp_path"] / "search.db"
        self.seed(db_path, checks=None)
        self.run(search, db_path, grades_in_order("INFERIOR"))
        logged = (search["tmp_path"] / "s.log").read_text(encoding="utf-8")
        assert "no recorded 8/8 submission check" in logged

    def test_a_verified_hit_survives_a_reload(self, search):
        # The resolved checks have to come back out of the database, or every
        # stored alpha would reload as unverified.
        db_path = search["tmp_path"] / "search.db"
        self.seed(db_path, checks=all_pass_checks())
        with ResultStore(db_path) as store:
            reloaded = store.find_by_grade("GOOD")[0]
        assert reloaded.submission_checks is not None
        assert reloaded.is_submittable is True


class TestSettingOverrides:
    def test_coerces_integers(self):
        assert search_alpha.parse_setting_overrides(["decay=8"]) == {"decay": 8}

    def test_coerces_floats(self):
        assert search_alpha.parse_setting_overrides(["truncation=0.05"]) == {"truncation": 0.05}

    def test_coerces_booleans(self):
        parsed = search_alpha.parse_setting_overrides(["visualization=false", "nanHandling=TRUE"])
        assert parsed == {"visualization": False, "nanHandling": True}

    def test_leaves_plain_strings_alone(self):
        parsed = search_alpha.parse_setting_overrides(["neutralization=SUBINDUSTRY"])
        assert parsed == {"neutralization": "SUBINDUSTRY"}

    def test_trims_whitespace(self):
        assert search_alpha.parse_setting_overrides([" decay = 8 "]) == {"decay": 8}

    def test_multiple_pairs_accumulate(self):
        parsed = search_alpha.parse_setting_overrides(["decay=8", "region=CHN"])
        assert parsed == {"decay": 8, "region": "CHN"}

    def test_value_containing_equals_keeps_the_tail(self):
        assert search_alpha.parse_setting_overrides(["tag=a=b"]) == {"tag": "a=b"}

    def test_pair_without_equals_raises(self):
        with pytest.raises(ConfigError) as excinfo:
            search_alpha.parse_setting_overrides(["decay"])
        assert "decay" in str(excinfo.value)
        assert "KEY=VALUE" in str(excinfo.value)

    def test_empty_key_raises(self):
        with pytest.raises(ConfigError):
            search_alpha.parse_setting_overrides(["=8"])

    def test_unknown_keys_pass_through(self):
        assert search_alpha.parse_setting_overrides(["someFutureFlag=1"]) == {"someFutureFlag": 1}

    def test_setting_reaches_the_simulation_payload(self, search):
        backend = grades_in_order("AVERAGE")
        code = search["run"](
            "--target-grade", "AVERAGE", "--max-attempts", "1",
            "--setting", "decay=8", "--setting", "neutralization=SUBINDUSTRY",
            backend=backend,
        )
        assert code == search_alpha.EXIT_FOUND
        payload = backend.submitted_payloads[0]
        assert payload["settings"]["decay"] == 8
        assert payload["settings"]["neutralization"] == "SUBINDUSTRY"
        assert payload["type"] == "REGULAR"

    def test_malformed_setting_is_a_usage_error(self, search):
        assert search["run"]("--setting", "nonsense") == search_alpha.EXIT_USAGE_ERROR


class TestCandidateSource:
    def test_input_file_supplies_the_candidates(self, search, tmp_path):
        source = tmp_path / "cands.txt"
        source.write_text("rank(a)\nrank(b)\nrank(c)\n", encoding="utf-8")
        backend = grades_in_order("INFERIOR", "AVERAGE", "INFERIOR")

        code = search["run"]("--input", str(source), "--target-grade", "AVERAGE",
                             "--max-attempts", "10", backend=backend)
        assert code == search_alpha.EXIT_FOUND
        assert backend.submissions == 2

    def test_missing_input_file_is_a_usage_error(self, search, tmp_path):
        assert search["run"]("--input", str(tmp_path / "nope.txt")) == search_alpha.EXIT_USAGE_ERROR

    def test_missing_credentials_is_a_usage_error(self, search, monkeypatch):
        monkeypatch.delenv("WQBRAIN_USERNAME", raising=False)
        monkeypatch.delenv("WQBRAIN_PASSWORD", raising=False)
        assert search["run"]("--target-grade", "AVERAGE") == search_alpha.EXIT_USAGE_ERROR


class TestConcurrencyCap:
    def test_requested_concurrency_is_clamped_to_three(self, search):
        backend = grades_in_order("AVERAGE")
        assert search["run"]("--concurrency", "25", "--target-grade", "AVERAGE",
                             "--max-attempts", "1", backend=backend) == 0
        assert backend.submissions == 1

    def test_min_request_interval_reaches_the_rate_limiter(self, search, monkeypatch):
        # This flag is the documented remedy for HTTP 429, so it must actually
        # reach the client rather than being silently dropped.
        seen = {}
        backend = grades_in_order("AVERAGE")
        # install() patches WorldQuantClient itself, so it must happen first;
        # the spy then wraps the fixture's factory rather than being replaced by it.
        search["install"](backend)
        fixture_factory = search_alpha.WorldQuantClient

        def spy(credentials, **kwargs):
            seen["min_request_interval"] = kwargs.get("min_request_interval")
            return fixture_factory(credentials, **kwargs)

        monkeypatch.setattr(search_alpha, "WorldQuantClient", spy)
        search_alpha.main([
            "--target-grade", "AVERAGE", "--max-attempts", "1",
            "--min-request-interval", "7.5",
            "--db", str(search["tmp_path"] / "s.db"),
            "--log-file", str(search["tmp_path"] / "s.log"),
            "--concurrency", "1", "--no-yearly",
        ])
        assert seen["min_request_interval"] == 7.5


class TestHistoricalAlphasResearchGate:
    """--include-existing routes stored alphas through evaluate_acceptance.

    A historical alpha that passes BRAIN's official 0.70 check must still
    clear the Registry's research gate (0.65): corr FAIL and corr UNKNOWN are
    not hits, while correlation.enabled=false yields NOT_APPLICABLE and the
    official checks alone decide.
    """

    def _seed(self, search, *, self_corr, alpha_id="OLD1", expression="rank(stored)"):
        from worldquant.hashing import dedup_key
        from worldquant.models import AlphaResult

        db_path = search["tmp_path"] / "search.db"
        settings = {"region": "USA", "delay": 1}
        with ResultStore(db_path) as store:
            alpha_row = store.upsert_alpha(expression, settings, name="stored")
            sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim_old")
            store.update_simulation(
                sim_row, status=SimulationStatus.COMPLETED,
                remote_alpha_id=alpha_id, mark_completed=True,
            )
            store.save_result(sim_row, AlphaResult(
                alpha_id="stored", expression=expression,
                dedup_key=dedup_key(expression, settings),
                status=SimulationStatus.COMPLETED, remote_alpha_id=alpha_id,
                sharpe=2.2, fitness=1.6, grade="GOOD",
                long_count=1500, short_count=1400, passed=True,
                submission_checks=all_pass_checks(),
                self_correlation=self_corr,
            ))
        return db_path

    def _registry_config(self, search, *, corr_enabled=True):
        cfg = search["tmp_path"] / "registry.yaml"
        lines = ["factor_registry:", "  enabled: true",
                 "  db_path: factor_registry.db"]
        if not corr_enabled:
            lines += ["  correlation:", "    enabled: false"]
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cfg

    def _other_candidates(self, search, expression="rank(ts_delta(volume, 5))"):
        path = search["tmp_path"] / "candidates.txt"
        path.write_text(expression + "\n", encoding="utf-8")
        return path

    def _log(self, search):
        return (search["tmp_path"] / "search.log").read_text(encoding="utf-8")

    def test_068_historical_alpha_is_not_a_hit(self, search):
        db_path = self._seed(search, self_corr=0.68, alpha_id="OLD68")
        cfg = self._registry_config(search)
        candidates = self._other_candidates(search)
        backend = grades_in_order("INFERIOR")
        code = search["run"](
            "--target-grade", "GOOD", "--include-existing",
            "--config", str(cfg), "--input", str(candidates),
            "--max-attempts", "1", backend=backend,
        )
        assert code == search_alpha.EXIT_NOT_FOUND
        # The stored alpha did not short-circuit: one fresh simulation ran.
        assert backend.submissions == 1
        logged = self._log(search)
        assert "rejected by the local research gate" in logged
        assert "research correlation FAIL" in logged

    def test_unknown_corr_historical_alpha_is_not_a_hit(self, search):
        # Official checks all PASS (that is why it is submittable), but no local
        # correlation evidence exists at all -> UNKNOWN -> not a hit.
        db_path = self._seed(search, self_corr=None, alpha_id="OLDUNK")
        cfg = self._registry_config(search)
        candidates = self._other_candidates(search)
        backend = grades_in_order("INFERIOR")
        code = search["run"](
            "--target-grade", "GOOD", "--include-existing",
            "--config", str(cfg), "--input", str(candidates),
            "--max-attempts", "1", backend=backend,
        )
        assert code == search_alpha.EXIT_NOT_FOUND
        assert backend.submissions == 1
        logged = self._log(search)
        assert "research correlation UNKNOWN" in logged

    def test_passing_historical_alpha_short_circuits_with_registry(self, search):
        db_path = self._seed(search, self_corr=0.31, alpha_id="OLD31")
        cfg = self._registry_config(search)

        class Exploding:
            def __call__(self, method, url, kwargs):
                raise AssertionError("must not simulate when a stored hit clears the gate")

        search["install"](Exploding())
        code = search_alpha.main([
            "--target-grade", "GOOD", "--include-existing",
            "--config", str(cfg), "--max-attempts", "1",
            "--db", str(db_path),
            "--log-file", str(search["tmp_path"] / "s2.log"),
            "--concurrency", "1", "--no-yearly",
        ])
        assert code == search_alpha.EXIT_FOUND

    def test_disabled_corr_gate_accepts_historical_alpha(self, search):
        import sqlite3

        db_path = self._seed(search, self_corr=0.68, alpha_id="OLD68B")
        cfg = self._registry_config(search, corr_enabled=False)

        class Exploding:
            def __call__(self, method, url, kwargs):
                raise AssertionError("no simulation needed when gate is disabled")

        search["install"](Exploding())
        code = search_alpha.main([
            "--target-grade", "GOOD", "--include-existing",
            "--config", str(cfg), "--max-attempts", "1",
            "--db", str(db_path),
            "--log-file", str(search["tmp_path"] / "s3.log"),
            "--concurrency", "1", "--no-yearly",
        ])
        assert code == search_alpha.EXIT_FOUND
        # The folded factor is PASSED with an explicit NOT_APPLICABLE verdict,
        # not SIMULATED/UNKNOWN.
        reg_db = search["tmp_path"] / "data" / "factor_registry.db"
        with sqlite3.connect(reg_db) as conn:
            row = conn.execute(
                "SELECT status, corr_status FROM factors "
                "WHERE brain_alpha_id = ?", ("OLD68B",),
            ).fetchone()
        assert row == ("PASSED", "NOT_APPLICABLE")


class TestAblationScopeDedup:
    """Ablation variants survive the scope_hash prefilter; normal rows don't."""

    def test_ablation_variant_bypasses_scope_prefilter(self, search):
        from worldquant.hashing import dedup_key
        from worldquant.models import AlphaResult

        expression = "rank(scope_sweep_probe)"
        db_path = search["tmp_path"] / "search.db"
        settings = {"region": "USA", "delay": 1}
        # The signal scope (region/universe/delay) was already simulated once.
        with ResultStore(db_path) as store:
            alpha_row = store.upsert_alpha(expression, settings, name="probe")
            sim_row = store.create_simulation(alpha_row, remote_simulation_id="sim_seed")
            store.update_simulation(
                sim_row, status=SimulationStatus.COMPLETED,
                remote_alpha_id="SCOPE1", mark_completed=True,
            )
            store.save_result(sim_row, AlphaResult(
                alpha_id="probe", expression=expression,
                dedup_key=dedup_key(expression, settings),
                status=SimulationStatus.COMPLETED, remote_alpha_id="SCOPE1",
                grade="INFERIOR", sharpe=0.4, fitness=0.3,
                submission_checks=all_pass_checks(), self_correlation=0.2,
            ))

        cfg = search["tmp_path"] / "registry.yaml"
        cfg.write_text(
            "factor_registry:\n  enabled: true\n  db_path: factor_registry.db\n",
            encoding="utf-8",
        )
        # Same expression/scope twice: the ablation row (decay=8) must survive
        # the scope prefilter; the ordinary row (decay=16) must be dropped.
        candidates = search["tmp_path"] / "sweep.csv"
        candidates.write_text(
            "expression,source,ablation_group_id,decay\n"
            f"{expression},ablation,g1,8\n"
            f"{expression},,,16\n",
            encoding="utf-8",
        )
        backend = grades_in_order("INFERIOR", "INFERIOR")
        code = search["run"](
            "--target-grade", "GOOD",
            "--config", str(cfg), "--input", str(candidates),
            "--max-attempts", "5", backend=backend,
        )
        assert code == search_alpha.EXIT_NOT_FOUND
        # Exactly one candidate (the ablation) reached BRAIN.
        assert backend.submissions == 1
        logged = (search["tmp_path"] / "search.log").read_text(encoding="utf-8")
        assert "Skipped 1 candidate" in logged
