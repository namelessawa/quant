"""Duplicate detection: an already-finished alpha is never submitted twice."""

from __future__ import annotations

from conftest import FakeClient, make_config, make_runner
from worldquant.api import SimulationStatus
from worldquant.hashing import dedup_key
from worldquant.models import AlphaSpec

SETTINGS = {"region": "USA", "delay": 1}


def run_once(store, tmp_path, expression="rank(close)", settings=SETTINGS, force=False):
    """Run one alpha with a fresh client, returning (result, client)."""
    config = make_config(tmp_path, settings=settings)
    client = FakeClient()
    runner, _ = make_runner(client, store, config)
    return runner.run_alpha(expression, settings, force=force), client


class TestSkipCompleted:
    def test_first_run_submits(self, store, tmp_path):
        result, client = run_once(store, tmp_path)
        assert result.status == SimulationStatus.COMPLETED
        assert len(client.submitted) == 1

    def test_identical_rerun_is_skipped(self, store, tmp_path):
        first, _ = run_once(store, tmp_path)
        second, client = run_once(store, tmp_path)
        assert client.submitted == [], "a completed alpha must not be submitted again"
        assert client.polled == []
        assert second.remote_alpha_id == first.remote_alpha_id
        assert second.sharpe == first.sharpe

    def test_whitespace_only_difference_is_skipped(self, store, tmp_path):
        run_once(store, tmp_path, expression="rank(ts_delta(close, 5))")
        _, client = run_once(store, tmp_path, expression="rank( ts_delta(close,5) )")
        assert client.submitted == []

    def test_different_expression_is_not_skipped(self, store, tmp_path):
        run_once(store, tmp_path, expression="rank(close)")
        _, client = run_once(store, tmp_path, expression="rank(volume)")
        assert len(client.submitted) == 1

    def test_different_settings_are_not_skipped(self, store, tmp_path):
        run_once(store, tmp_path, settings={"region": "USA", "delay": 1})
        _, client = run_once(store, tmp_path, settings={"region": "CHN", "delay": 1})
        assert len(client.submitted) == 1
        assert client.submitted[0][1]["region"] == "CHN"

    def test_snake_case_settings_match_camel_case(self, store, tmp_path):
        run_once(store, tmp_path, settings={"region": "USA", "unit_handling": "VERIFY"})
        _, client = run_once(store, tmp_path, settings={"region": "USA", "unitHandling": "VERIFY"})
        assert client.submitted == []

    def test_force_reruns_a_completed_alpha(self, store, tmp_path):
        run_once(store, tmp_path)
        forced, client = run_once(store, tmp_path, force=True)
        assert len(client.submitted) == 1
        assert forced.status == SimulationStatus.COMPLETED
        # The first result is still in the database; nothing was overwritten.
        assert len(store.all_results()) == 2

    def test_failed_run_is_retried_on_the_next_invocation(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        failing = FakeClient(status_script=[
            {"status": SimulationStatus.FAILED, "alpha_id": None, "progress": None,
             "message": "parse error"},
        ])
        runner, _ = make_runner(failing, store, config)
        first = runner.run_alpha("rank(close)", SETTINGS)
        assert first.status == SimulationStatus.FAILED

        # A failure is not a completed result, so the next run tries again.
        second, client = run_once(store, tmp_path)
        assert len(client.submitted) == 1
        assert second.status == SimulationStatus.COMPLETED


class TestBatchDeduplication:
    def test_duplicates_within_one_batch_submit_once(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient()
        runner, _ = make_runner(client, store, config)
        specs = [
            AlphaSpec(expression="rank(close)", name="a", settings=SETTINGS),
            AlphaSpec(expression="rank( close )", name="b", settings=SETTINGS),
        ]
        results = runner.run_batch(specs)
        assert len(results) == 2
        assert len(client.submitted) == 1
        assert results[1].status == SimulationStatus.COMPLETED

    def test_second_batch_skips_what_the_first_completed(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        expressions = ["rank(close)", "rank(volume)", "rank(returns)"]

        first_client = FakeClient()
        first_runner, _ = make_runner(first_client, store, config)
        first_runner.run_batch(expressions)
        assert len(first_client.submitted) == 3

        second_client = FakeClient()
        second_runner, _ = make_runner(second_client, store, config)
        results = second_runner.run_batch(expressions)
        assert second_client.submitted == []
        assert all(r.status == SimulationStatus.COMPLETED for r in results)

    def test_partially_completed_batch_only_runs_the_remainder(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        first_client = FakeClient()
        first_runner, _ = make_runner(first_client, store, config)
        first_runner.run_batch(["rank(close)", "rank(volume)"])

        second_client = FakeClient()
        second_runner, _ = make_runner(second_client, store, config)
        second_runner.run_batch(["rank(volume)", "rank(returns)"])
        assert [call[0] for call in second_client.submitted] == ["rank(returns)"]

    def test_limit_caps_how_many_are_run(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient()
        runner, _ = make_runner(client, store, config)
        runner.run_batch(["rank(a)", "rank(b)", "rank(c)"], limit=2)
        assert len(client.submitted) == 2


class TestDedupKeyStorage:
    def test_stored_dedup_key_matches_the_expression_and_settings(self, store, tmp_path):
        result, _ = run_once(store, tmp_path, expression="rank( close )")
        expected = dedup_key("rank(close)", SETTINGS)
        assert result.dedup_key == expected
        assert store.find_alpha_by_key(expected) is not None

    def test_two_settings_produce_two_alpha_rows(self, store, tmp_path):
        run_once(store, tmp_path, settings={"region": "USA", "delay": 1})
        run_once(store, tmp_path, settings={"region": "GLB", "delay": 1})
        rows = store._query("SELECT dedup_key FROM alphas")
        assert len(rows) == 2
        assert len({row["dedup_key"] for row in rows}) == 2
