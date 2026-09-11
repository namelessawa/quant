"""Polling cadence, timeouts and remote failures.

Uses a virtual clock, so a 1800s timeout is exercised instantly.
"""

from __future__ import annotations

import pytest

from conftest import FakeClient, make_config, make_runner
from worldquant.api import SimulationStatus
from worldquant.config import RunnerConfig
from worldquant.exceptions import APIError, SimulationFailedError
from worldquant.simulator import MAX_POLL_DELAY, MIN_POLL_DELAY

SETTINGS = {"region": "USA", "delay": 1}

RUNNING = {
    "status": SimulationStatus.RUNNING,
    "alpha_id": None,
    "progress": 0.5,
    "message": None,
    "retry_after": None,
}


def always_running(simulation_id: str, poll_index: int) -> dict:
    return dict(RUNNING)


def runner_with(handler, tmp_path, store, *, max_wait=30.0, poll_interval=1.0, poll_jitter=0.0,
                concurrency=1, **kwargs):
    config = make_config(
        tmp_path,
        settings=SETTINGS,
        runner=RunnerConfig(
            poll_interval=poll_interval, poll_jitter=poll_jitter, max_wait=max_wait,
            concurrency=concurrency, min_request_interval=0.0,
        ),
        **kwargs,
    )
    client = FakeClient(status_handler=handler)
    runner, clock = make_runner(client, store, config)
    return runner, client, clock, config


class TestTimeout:
    def test_marks_the_result_as_timeout(self, store, tmp_path):
        runner, client, _, _ = runner_with(always_running, tmp_path, store, max_wait=20.0)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.status == SimulationStatus.TIMEOUT
        assert result.passed is False
        assert result.sharpe is None

    def test_error_message_names_the_budget_and_the_simulation(self, store, tmp_path):
        runner, _, _, _ = runner_with(always_running, tmp_path, store, max_wait=20.0)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert "20s" in result.error
        assert result.simulation_id in result.error

    def test_timeout_state_is_persisted_for_resume(self, store, tmp_path):
        runner, client, _, _ = runner_with(always_running, tmp_path, store, max_wait=10.0)
        runner.run_alpha("rank(close)", SETTINGS)

        rows = store._query("SELECT * FROM simulations")
        assert len(rows) == 1
        assert rows[0]["status"] == SimulationStatus.TIMEOUT
        assert rows[0]["completed_at"]
        # The remote id survives, so the run can be inspected or resumed.
        assert len(client.submitted) == 1
        assert rows[0]["remote_simulation_id"] == "sim_0001"

    def test_timeout_is_written_to_failed_csv_not_dropped(self, store, tmp_path):
        runner, _, _, _ = runner_with(always_running, tmp_path, store, max_wait=10.0)
        runner.run_alpha("rank(close)", SETTINGS)
        paths = store.export_all(tmp_path)
        content = paths["failed"].read_text(encoding="utf-8")
        assert SimulationStatus.TIMEOUT in content

    def test_zero_budget_times_out_after_the_first_poll(self, store, tmp_path):
        runner, client, _, _ = runner_with(always_running, tmp_path, store, max_wait=0.0)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.status == SimulationStatus.TIMEOUT
        assert client.poll_counts[result.simulation_id] == 1

    def test_no_alpha_lookup_after_a_timeout(self, store, tmp_path):
        runner, client, _, _ = runner_with(always_running, tmp_path, store, max_wait=10.0)
        runner.run_alpha("rank(close)", SETTINGS)
        assert client.fetched_alphas == []


class TestPollingCadence:
    def test_never_polls_faster_than_the_floor(self, store, tmp_path):
        runner, _, clock, _ = runner_with(
            always_running, tmp_path, store, max_wait=60.0, poll_interval=0.0
        )
        runner.run_alpha("rank(close)", SETTINGS)
        assert clock.sleeps, "expected the poller to sleep between attempts"
        assert all(delay >= MIN_POLL_DELAY for delay in clock.sleeps)

    def test_waits_at_least_the_configured_budget(self, store, tmp_path):
        runner, _, clock, _ = runner_with(always_running, tmp_path, store, max_wait=45.0)
        runner.run_alpha("rank(close)", SETTINGS)
        assert clock.total_slept >= 45.0

    def test_single_sleep_never_exceeds_the_ceiling(self, store, tmp_path):
        runner, _, clock, _ = runner_with(
            always_running, tmp_path, store, max_wait=500.0, poll_interval=9999.0
        )
        runner.run_alpha("rank(close)", SETTINGS)
        assert all(delay <= MAX_POLL_DELAY for delay in clock.sleeps)

    def test_server_retry_after_sets_the_poll_delay(self, store, tmp_path):
        def handler(simulation_id, poll_index):
            entry = dict(RUNNING)
            entry["retry_after"] = 7.0
            return entry

        runner, _, clock, _ = runner_with(handler, tmp_path, store, max_wait=30.0)
        runner.run_alpha("rank(close)", SETTINGS)
        assert 7.0 in clock.sleeps

    def test_retry_after_zero_falls_back_to_the_interval(self, store, tmp_path):
        def handler(simulation_id, poll_index):
            entry = dict(RUNNING)
            entry["retry_after"] = 0.0
            return entry

        runner, _, clock, _ = runner_with(
            handler, tmp_path, store, max_wait=20.0, poll_interval=5.0
        )
        runner.run_alpha("rank(close)", SETTINGS)
        assert 5.0 in clock.sleeps

    def test_jitter_spreads_the_polls(self, store, tmp_path):
        runner, _, clock, _ = runner_with(
            always_running, tmp_path, store, max_wait=120.0, poll_interval=1.0, poll_jitter=4.0
        )
        runner.run_alpha("rank(close)", SETTINGS)
        assert len(set(clock.sleeps)) > 1, "jitter should make delays vary"
        assert all(1.0 <= delay <= 5.0 for delay in clock.sleeps)

    def test_status_moves_to_running_in_the_database(self, store, tmp_path):
        seen: list[str] = []

        def handler(simulation_id, poll_index):
            if poll_index == 0:
                return dict(RUNNING)
            seen.append(simulation_id)
            return {
                "status": SimulationStatus.COMPLETED,
                "alpha_id": "Xp2Kd", "progress": 1.0, "message": None, "retry_after": None,
            }

        runner, _, _, _ = runner_with(handler, tmp_path, store, max_wait=60.0)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.status == SimulationStatus.COMPLETED
        assert seen, "expected at least one running poll before completion"


class TestRemoteFailure:
    def test_fail_status_is_recorded_with_the_server_message(self, store, tmp_path):
        def handler(simulation_id, poll_index):
            return {
                "status": SimulationStatus.FAILED, "alpha_id": None, "progress": None,
                "message": "unit handling mismatch: close [m]", "retry_after": None,
            }

        runner, client, _, _ = runner_with(handler, tmp_path, store, max_wait=60.0)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.status == SimulationStatus.FAILED
        assert "unit handling mismatch" in result.error
        assert client.fetched_alphas == []

    def test_failure_is_persisted(self, store, tmp_path):
        def handler(simulation_id, poll_index):
            return {
                "status": SimulationStatus.FAILED, "alpha_id": None, "progress": None,
                "message": "boom", "retry_after": None,
            }

        runner, _, _, _ = runner_with(handler, tmp_path, store)
        runner.run_alpha("rank(close)", SETTINGS)
        row = store._query("SELECT * FROM simulations")[0]
        assert row["status"] == SimulationStatus.FAILED
        # The stored error keeps the server message plus enough context to
        # diagnose it without re-running the batch.
        assert "boom" in row["error"]
        assert "rank(close)" in row["error"]

    def test_failure_reason_reaches_the_filter_verdict(self, store, tmp_path):
        def handler(simulation_id, poll_index):
            return {
                "status": SimulationStatus.FAILED, "alpha_id": None, "progress": None,
                "message": "boom", "retry_after": None,
            }

        runner, _, _, _ = runner_with(handler, tmp_path, store)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.passed is False
        assert result.reasons and "boom" in result.reasons[0]

    def test_exception_type_is_the_documented_one(self):
        assert issubclass(SimulationFailedError, Exception)


class TestRequestFailure:
    def test_persistent_api_error_becomes_request_error(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(poll_error=APIError("gateway timeout", status_code=504, url="/simulations/x"))
        runner, _ = make_runner(client, store, config)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.status == SimulationStatus.REQUEST_ERROR
        assert "gateway timeout" in result.error

    def test_request_error_state_is_persisted(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(poll_error=APIError("boom", url="/x"))
        runner, _ = make_runner(client, store, config)
        runner.run_alpha("rank(close)", SETTINGS)
        assert store._query("SELECT * FROM simulations")[0]["status"] == SimulationStatus.REQUEST_ERROR

    def test_submit_failure_does_not_raise(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(submit_error=APIError("no location", status_code=201, url="/simulations"))
        runner, _ = make_runner(client, store, config)
        result = runner.run_alpha("rank(close)", SETTINGS)
        assert result.status == SimulationStatus.REQUEST_ERROR
        assert "no location" in result.error


class TestBatchIsolation:
    def test_one_failure_does_not_stop_the_batch(self, store, tmp_path):
        def handler(simulation_id, poll_index):
            if simulation_id.endswith("2"):
                return {
                    "status": SimulationStatus.FAILED, "alpha_id": None, "progress": None,
                    "message": "bad expression", "retry_after": None,
                }
            return {
                "status": SimulationStatus.COMPLETED, "alpha_id": f"alpha_{simulation_id}",
                "progress": 1.0, "message": None, "retry_after": None,
            }

        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(simulation_ids=["sim1", "sim2", "sim3"], status_handler=handler)
        runner, _ = make_runner(client, store, config)
        results = runner.run_batch(["rank(a)", "rank(b)", "rank(c)"])

        assert len(results) == 3
        assert [r.status for r in results] == [
            SimulationStatus.COMPLETED, SimulationStatus.FAILED, SimulationStatus.COMPLETED,
        ]
        assert not runner.halted

    def test_results_keep_input_order_under_concurrency(self, store, tmp_path):
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(
                poll_interval=1.0, poll_jitter=0.0, max_wait=60.0,
                concurrency=3, min_request_interval=0.0,
            ),
        )
        client = FakeClient()
        runner, _ = make_runner(client, store, config)
        expressions = [f"rank(field_{i})" for i in range(6)]
        results = runner.run_batch(expressions)
        assert [r.expression for r in results] == expressions

    def test_concurrent_batch_completes_every_alpha(self, store, tmp_path):
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(
                poll_interval=1.0, poll_jitter=0.0, max_wait=60.0,
                concurrency=3, min_request_interval=0.0,
            ),
        )
        client = FakeClient()
        runner, _ = make_runner(client, store, config)
        results = runner.run_batch([f"rank(f{i})" for i in range(9)])
        assert len(results) == 9
        assert all(r.status == SimulationStatus.COMPLETED for r in results)
        assert len(store.all_results()) == 9

    def test_empty_batch_is_handled(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        runner, _ = make_runner(FakeClient(), store, config)
        assert runner.run_batch([]) == []

    def test_unsupported_batch_item_type_raises(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        runner, _ = make_runner(FakeClient(), store, config)
        with pytest.raises(TypeError):
            runner.run_batch([123])  # type: ignore[list-item]
