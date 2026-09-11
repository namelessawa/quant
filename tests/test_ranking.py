"""Leaderboard ordering and rendering."""

from __future__ import annotations

from conftest import submission_checks as all_pass_checks
from worldquant.api import SimulationStatus
from worldquant.models import AlphaResult
from worldquant.ranking import (
    format_leaderboard,
    latest_per_alpha,
    rank_results,
    year_stability_label,
)


def result(alpha_id, *, fitness=None, sharpe=None, turnover=None, drawdown=None,
           status=SimulationStatus.COMPLETED, expression=None, yearly=None):
    return AlphaResult(
        alpha_id=alpha_id,
        expression=expression or f"rank({alpha_id})",
        dedup_key=alpha_id,
        status=status,
        fitness=fitness,
        sharpe=sharpe,
        turnover=turnover,
        drawdown=drawdown,
        yearly_stats=yearly or {},
    )


class TestOrdering:
    def test_fitness_descends_first(self):
        ranked = rank_results([
            result("low", fitness=0.5, sharpe=9.0),
            result("high", fitness=2.0, sharpe=0.1),
        ])
        assert [r.alpha_id for r in ranked] == ["high", "low"]

    def test_sharpe_breaks_a_fitness_tie(self):
        ranked = rank_results([
            result("a", fitness=1.0, sharpe=0.5),
            result("b", fitness=1.0, sharpe=2.0),
        ])
        assert [r.alpha_id for r in ranked] == ["b", "a"]

    def test_turnover_breaks_a_sharpe_tie(self):
        ranked = rank_results([
            result("churn", fitness=1.0, sharpe=1.0, turnover=0.9),
            result("calm", fitness=1.0, sharpe=1.0, turnover=0.2),
        ])
        assert [r.alpha_id for r in ranked] == ["calm", "churn"]

    def test_drawdown_is_the_final_tiebreak(self):
        ranked = rank_results([
            result("deep", fitness=1.0, sharpe=1.0, turnover=0.3, drawdown=0.4),
            result("shallow", fitness=1.0, sharpe=1.0, turnover=0.3, drawdown=0.05),
        ])
        assert [r.alpha_id for r in ranked] == ["shallow", "deep"]

    def test_missing_fitness_sorts_last(self):
        ranked = rank_results([
            result("none", fitness=None, sharpe=9.0),
            result("measured", fitness=0.1, sharpe=0.1),
        ])
        assert [r.alpha_id for r in ranked] == ["measured", "none"]

    def test_missing_turnover_sorts_last_within_its_tier(self):
        ranked = rank_results([
            result("unknown_turnover", fitness=1.0, sharpe=1.0, turnover=None),
            result("known_turnover", fitness=1.0, sharpe=1.0, turnover=0.99),
        ])
        assert [r.alpha_id for r in ranked] == ["known_turnover", "unknown_turnover"]

    def test_incomplete_runs_are_excluded_by_default(self):
        ranked = rank_results([
            result("ok", fitness=1.0),
            result("failed", fitness=9.0, status=SimulationStatus.FAILED),
            result("timed_out", fitness=9.0, status=SimulationStatus.TIMEOUT),
        ])
        assert [r.alpha_id for r in ranked] == ["ok"]

    def test_incomplete_runs_can_be_included(self):
        ranked = rank_results(
            [result("ok", fitness=1.0), result("failed", fitness=9.0, status=SimulationStatus.FAILED)],
            only_completed=False,
        )
        assert [r.alpha_id for r in ranked] == ["failed", "ok"]

    def test_custom_sort_fields(self):
        ranked = rank_results(
            [result("a", sharpe=0.5, turnover=0.1), result("b", sharpe=2.0, turnover=0.9)],
            sort_fields=[("sharpe", True)],
        )
        assert [r.alpha_id for r in ranked] == ["b", "a"]

    def test_empty_input(self):
        assert rank_results([]) == []


class TestLatestPerAlpha:
    def test_repeated_runs_collapse_to_the_most_recent(self):
        stale = result("a", fitness=1.0, expression="rank(close)")
        stale.dedup_key = "same"
        fresh = result("a", fitness=2.0, expression="rank(close)")
        fresh.dedup_key = "same"

        collapsed = latest_per_alpha([stale, fresh])
        assert len(collapsed) == 1
        assert collapsed[0].fitness == 2.0

    def test_distinct_alphas_are_all_kept(self):
        first = result("a", fitness=1.0)
        first.dedup_key = "k1"
        second = result("b", fitness=2.0)
        second.dedup_key = "k2"

        assert len(latest_per_alpha([first, second])) == 2

    def test_newer_yearly_stats_win_over_a_stale_row(self):
        # A row written before the yearly-stats parser was fixed has no yearly
        # data; the re-run does. The leaderboard must show the richer one.
        stale = result("a", fitness=1.0)
        stale.dedup_key = "k"
        fresh = result("a", fitness=1.0)
        fresh.dedup_key = "k"
        fresh.yearly_stats = {"2019": {"sharpe": 1.0}, "2020": {"sharpe": -0.5}}

        collapsed = latest_per_alpha([stale, fresh])
        assert collapsed[0].yearly_summary.total_years == 2

    def test_falls_back_to_the_expression_when_no_key(self):
        first = result("a", fitness=1.0, expression="rank(close)")
        first.dedup_key = ""
        second = result("a", fitness=2.0, expression="rank(close)")
        second.dedup_key = ""

        assert len(latest_per_alpha([first, second])) == 1

    def test_input_order_is_preserved(self):
        rows = []
        for index, key in enumerate(("k1", "k2", "k3")):
            item = result(f"a{index}", fitness=float(index))
            item.dedup_key = key
            rows.append(item)

        assert [r.alpha_id for r in latest_per_alpha(rows)] == ["a0", "a1", "a2"]

    def test_empty_input(self):
        assert latest_per_alpha([]) == []


class TestRendering:
    def test_header_lists_the_documented_columns(self):
        table = format_leaderboard([result("a", fitness=1.0, sharpe=1.0, turnover=0.4)])
        for column in ("Rank", "Alpha ID", "Grade", "Expression", "Sharpe", "Fitness",
                       "Turnover", "Returns", "Drawdown", "Margin", "Year Stability"):
            assert column in table

    def test_official_grade_is_rendered(self):
        row = result("a", fitness=1.0, sharpe=1.0)
        row.grade = "GOOD"
        assert "GOOD" in format_leaderboard([row])

    def test_the_header_lists_the_submission_column(self):
        table = format_leaderboard([result("a", fitness=1.0, sharpe=1.0)])
        assert "Subm" in table.splitlines()[1]

    def test_submission_verdict_is_rendered(self):
        row = result("a", fitness=1.53, sharpe=1.84)
        row.grade = "GOOD"
        row.submission_checks = all_pass_checks()
        assert "8/8" in format_leaderboard([row])

    def test_a_partially_failed_verdict_is_rendered(self):
        row = result("a", fitness=1.2, sharpe=1.4)
        row.submission_checks = all_pass_checks({"SELF_CORRELATION": "FAIL"})
        assert "7/8" in format_leaderboard([row])

    def test_never_checked_renders_as_a_dash(self):
        lines = format_leaderboard([result("a", fitness=1.2, sharpe=1.4)]).splitlines()
        assert lines[3].split()[3] == "-"

    def test_an_unsubmittable_leader_is_distinguishable_from_a_submittable_one(self):
        # The reason the column exists: ranking follows fitness, so the top of the
        # table can be an alpha that cannot be submitted at all.
        stronger = result("rich", fitness=2.69, sharpe=2.28)
        stronger.grade = "SPECTACULAR"
        stronger.submission_checks = all_pass_checks({"SELF_CORRELATION": "FAIL"})
        usable = result("poorer", fitness=1.53, sharpe=1.84)
        usable.grade = "GOOD"
        usable.submission_checks = all_pass_checks()

        lines = format_leaderboard([stronger, usable]).splitlines()
        assert lines[3].split()[1] == "rich" and lines[3].split()[3] == "7/8"
        assert lines[4].split()[1] == "poorer" and lines[4].split()[3] == "8/8"

    def test_missing_grade_renders_as_a_dash(self):
        row = result("a", fitness=1.0, sharpe=1.0)
        row.grade = None
        lines = format_leaderboard([row]).splitlines()
        assert lines[3].split()[2] == "-"

    def test_overlong_grade_does_not_break_the_layout(self):
        row = result("a", fitness=1.0, sharpe=1.0)
        row.grade = "SOMETHING_UNEXPECTEDLY_LONG"
        lines = format_leaderboard([row]).splitlines()
        # Truncation to the column width must keep every line the same length.
        assert len(lines[1]) == len(lines[3])

    def test_top_limits_the_row_count(self):
        rows = [result(f"a{i}", fitness=float(i)) for i in range(10)]
        table = format_leaderboard(rows, top=3)
        assert table.startswith("Top 3 Alpha")
        # title + header + separator + 3 data rows
        assert len(table.splitlines()) == 6

    def test_empty_input_message(self):
        assert format_leaderboard([]) == "No completed alphas to rank."

    def test_turnover_is_shown_as_a_percent(self):
        table = format_leaderboard([result("a", fitness=1.0, turnover=0.423)])
        assert "42.3%" in table

    def test_missing_values_render_as_dashes(self):
        table = format_leaderboard([result("a", fitness=1.0)])
        assert "-" in table
        assert "n/a" in table  # year stability

    def test_long_expressions_are_truncated_with_ascii_ellipsis(self):
        long_expression = "rank(" + "x" * 80 + ")"
        table = format_leaderboard([result("a", fitness=1.0, expression=long_expression)])
        assert "..." in table
        assert "\u2026" not in table
        assert long_expression not in table

    def test_rows_are_numbered_from_one(self):
        rows = [result(f"a{i}", fitness=float(i)) for i in range(3)]
        lines = format_leaderboard(rows, top=3).splitlines()
        data_rows = lines[3:]
        assert len(data_rows) == 3
        assert data_rows[0].lstrip().startswith("1")
        assert data_rows[-1].lstrip().startswith("3")

    def test_table_columns_stay_aligned(self):
        rows = [
            result("short", fitness=1.0, sharpe=1.0),
            result("a_much_longer_alpha_id", fitness=2.0, sharpe=2.0),
        ]
        lines = format_leaderboard(rows, top=2).splitlines()
        assert len({len(line) for line in lines[1:]}) == 1


class TestYearStabilityLabel:
    def test_reports_ratio_std_and_worst(self):
        yearly = {
            "2019": {"sharpe": 1.0}, "2020": {"sharpe": -0.5},
            "2021": {"sharpe": 1.0}, "2022": {"sharpe": 1.0},
        }
        label = year_stability_label(result("a", yearly=yearly))
        assert label.startswith("3/4+")
        assert "w=-0.50" in label

    def test_no_yearly_data(self):
        assert year_stability_label(result("a")) == "n/a"
