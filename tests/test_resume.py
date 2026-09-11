"""Resuming an interrupted batch — the state machine the spec calls out.

Crash scenario reproduced here::

    alpha1 COMPLETED
    alpha2 RUNNING   (has a remote simulation id)
    alpha3 PENDING   (row created, but the POST never returned)

After a restart alpha1 must not run again, alpha2 must be picked up by polling
its existing remote id, and alpha3 must be submitted fresh. No task is lost.
"""

from __future__ import annotations

import json

from conftest import FakeClient, FakeClock, make_config, make_runner
from worldquant.api import SimulationStatus
from worldquant.config import RunnerConfig
from worldquant.exceptions import AuthError, CaptchaRequiredError
from worldquant.hashing import dedup_key
from worldquant.models import AlphaResult, AlphaSpec
from worldquant.storage import utcnow_iso

SETTINGS = {"region": "USA", "delay": 1}

COMPLETED_METRICS = {
    "sharpe": 1.5, "fitness": 1.2, "turnover": 0.4, "returns": 0.12,
    "drawdown": 0.05, "margin": 0.0007, "pnl": 100.0, "book_size": 1000.0,
    "long_count": 1500, "short_count": 1500,
}


def seed_crash_state(store) -> dict[str, int]:
    """Write the database exactly as an interrupted run would have left it."""
    ids: dict[str, int] = {}

    # alpha1: finished, metrics stored.
    a1 = store.upsert_alpha("rank(close)", SETTINGS, name="alpha1")
    s1 = store.create_simulation(a1, remote_simulation_id="remote_1", status=SimulationStatus.SUBMITTED)
    store.update_simulation(
        s1, status=SimulationStatus.COMPLETED, remote_alpha_id="DONE1", mark_completed=True
    )
    store.save_result(
        s1,
        AlphaResult(
            alpha_id="alpha1", expression="rank(close)",
            dedup_key=dedup_key("rank(close)", SETTINGS),
            status=SimulationStatus.COMPLETED, simulation_id="remote_1",
            remote_alpha_id="DONE1", settings_json=json.dumps(SETTINGS),
            created_at=utcnow_iso(), completed_at=utcnow_iso(), **COMPLETED_METRICS,
        ),
    )
    ids["alpha1"] = a1

    # alpha2: submitted, still running remotely when the process died.
    a2 = store.upsert_alpha("rank(volume)", SETTINGS, name="alpha2")
    s2 = store.create_simulation(a2, remote_simulation_id="remote_2", status=SimulationStatus.SUBMITTED)
    store.update_simulation(s2, status=SimulationStatus.RUNNING)
    ids["alpha2"] = a2

    # alpha3: row created, POST never returned, so there is no remote id.
    a3 = store.upsert_alpha("rank(returns)", SETTINGS, name="alpha3")
    store.create_simulation(a3, status=SimulationStatus.PENDING)
    ids["alpha3"] = a3

    return ids


def specs() -> list[AlphaSpec]:
    return [
        AlphaSpec(expression="rank(close)", name="alpha1", settings=SETTINGS),
        AlphaSpec(expression="rank(volume)", name="alpha2", settings=SETTINGS),
        AlphaSpec(expression="rank(returns)", name="alpha3", settings=SETTINGS),
    ]


def completing_handler(simulation_id: str, poll_index: int) -> dict:
    return {
        "status": SimulationStatus.COMPLETED,
        "alpha_id": f"alpha_for_{simulation_id}",
        "progress": 1.0,
        "message": None,
        "retry_after": None,
    }


class TestResumeAfterCrash:
    def test_completed_alpha_is_not_rerun(self, store, tmp_path):
        seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        runner.run_batch(specs())
        assert [call[0] for call in client.submitted] == ["rank(returns)"]

    def test_running_alpha_resumes_its_existing_remote_id(self, store, tmp_path):
        seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        runner.run_batch(specs())
        assert "remote_2" in client.polled
        # Resumed, not resubmitted.
        assert "rank(volume)" not in [call[0] for call in client.submitted]

    def test_pending_alpha_without_remote_id_is_submitted_fresh(self, store, tmp_path):
        seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        runner.run_batch(specs())
        assert "rank(returns)" in [call[0] for call in client.submitted]

    def test_all_three_end_up_completed(self, store, tmp_path):
        seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        results = runner.run_batch(specs())
        assert len(results) == 3
        assert all(r.status == SimulationStatus.COMPLETED for r in results)
        assert store.status_counts()[SimulationStatus.COMPLETED] == 3

    def test_no_duplicate_simulation_rows_for_the_resumed_alpha(self, store, tmp_path):
        ids = seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        runner, _ = make_runner(FakeClient(status_handler=completing_handler), store, config)
        runner.run_batch(specs())

        rows = store._query(
            "SELECT * FROM simulations WHERE alpha_id = ?", (ids["alpha2"],)
        )
        assert len(rows) == 1, "resume must reuse the existing simulation row"

    def test_resumed_alpha_stores_the_new_remote_alpha_id(self, store, tmp_path):
        seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        runner, _ = make_runner(FakeClient(status_handler=completing_handler), store, config)
        runner.run_batch(specs())

        row = store._query(
            "SELECT remote_alpha_id FROM simulations WHERE remote_simulation_id = 'remote_2'"
        )[0]
        assert row["remote_alpha_id"] == "alpha_for_remote_2"


class TestResumeIncomplete:
    def test_finishes_in_flight_work_without_an_input_file(self, store, tmp_path):
        seed_crash_state(store)
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        results = runner.resume_incomplete()
        assert len(results) == 2
        assert all(r.status == SimulationStatus.COMPLETED for r in results)
        assert client.polled and "remote_2" in client.polled

    def test_reports_nothing_to_do_when_clean(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        runner, client = make_runner(FakeClient(), store, config)
        runner.run_batch(["rank(close)"])

        second = FakeClient(status_handler=completing_handler)
        runner2, _ = make_runner(second, store, config)
        assert runner2.resume_incomplete() == []
        assert second.submitted == []

    def test_repairs_a_pending_row_that_never_got_a_remote_id(self, store, tmp_path):
        alpha_row = store.upsert_alpha("rank(orphan)", SETTINGS, name="orphan")
        store.create_simulation(alpha_row, status=SimulationStatus.PENDING)

        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        results = runner.resume_incomplete()
        assert len(results) == 1
        assert results[0].status == SimulationStatus.COMPLETED
        assert [call[0] for call in client.submitted] == ["rank(orphan)"]

    def test_orphaned_row_with_a_stale_dedup_key_is_retired(self, store, tmp_path):
        # The live database was left in exactly this state: a row written before
        # the YAML ON/OFF coercion landed carries a dedup_key that the current
        # normalizer can never reproduce. run_spec resolves the expression to a
        # different (current) alpha row, so this one is never driven terminal and
        # would be picked up again on every single startup.
        #
        # Note that passing boolean vs string settings cannot recreate it: both
        # now normalize to the same canonical form and hash identically. The
        # stale key has to be written directly, as the older code did.
        settings = {"region": "USA", "pasteurization": "ON", "nanHandling": "OFF"}
        stale_alpha = store.upsert_alpha("rank(close)", settings, name="stale")
        store.create_simulation(stale_alpha, status=SimulationStatus.REQUEST_ERROR)
        store._execute(
            "UPDATE alphas SET dedup_key = ? WHERE id = ?",
            ("stale_key_from_an_older_normalizer", stale_alpha),
        )

        # The same alpha, already completed under its current key.
        fresh_alpha = store.upsert_alpha("rank(close)", settings, name="fresh")
        fresh_sim = store.create_simulation(fresh_alpha, remote_simulation_id="remote_done")
        store.update_simulation(
            fresh_sim, status=SimulationStatus.COMPLETED, remote_alpha_id="DONE",
            mark_completed=True,
        )
        assert stale_alpha != fresh_alpha, "a stale key must not collide with the current one"

        config = make_config(tmp_path, settings=settings)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        results = runner.resume_incomplete()
        assert len(results) == 1
        assert client.submitted == [], "must not resubmit an alpha that already completed"

        stale_rows = store._query(
            "SELECT * FROM simulations WHERE alpha_id = ?", (stale_alpha,)
        )
        assert len(stale_rows) == 1
        assert stale_rows[0]["status"] == SimulationStatus.SKIPPED
        assert "superseded" in stale_rows[0]["error"]
        assert stale_rows[0]["completed_at"]

        # And it must not churn again on the next startup.
        assert store.find_incomplete_simulations() == []
        assert runner.resume_incomplete() == []

    def test_settings_are_recovered_from_the_database(self, store, tmp_path):
        custom = {"region": "CHN", "delay": 0, "universe": "TOP1000"}
        alpha_row = store.upsert_alpha("rank(close)", custom, name="cn")
        store.create_simulation(alpha_row, remote_simulation_id="remote_cn",
                                status=SimulationStatus.RUNNING)

        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)
        runner.resume_incomplete()

        stored = json.loads(store.all_results()[0].settings_json)
        assert stored["region"] == "CHN"
        assert stored["delay"] == 0


class TestResumeAfterTimeout:
    """End-to-end: a run times out, a later run picks the same simulation up."""

    def test_timeout_then_resume_completes(self, store, tmp_path):
        config_first = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=1.0, poll_jitter=0.0, max_wait=5.0,
                                concurrency=1, min_request_interval=0.0),
        )
        running = {
            "status": SimulationStatus.RUNNING, "alpha_id": None, "progress": 0.3,
            "message": None, "retry_after": None,
        }
        first_client = FakeClient(
            simulation_ids=["remote_x"], status_handler=lambda sid, i: dict(running)
        )
        first_runner, _ = make_runner(first_client, store, config_first)
        first = first_runner.run_alpha("rank(close)", SETTINGS)
        assert first.status == SimulationStatus.TIMEOUT

        # The server eventually finished the work we stopped waiting for.
        second_client = FakeClient(status_handler=completing_handler)
        second_runner, _ = make_runner(second_client, store, make_config(tmp_path, settings=SETTINGS))
        resumed = second_runner.run_alpha("rank(close)", SETTINGS)

        assert resumed.status == SimulationStatus.COMPLETED
        assert second_client.submitted == [], "must not resubmit a timed-out simulation"
        assert "remote_x" in second_client.polled

    def test_resume_incomplete_repolls_a_timed_out_simulation(self, store, tmp_path):
        # TIMEOUT means *we* stopped waiting, not that the server stopped working.
        # Re-polling is cheap; resubmitting would burn quota and duplicate the alpha.
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=1.0, poll_jitter=0.0, max_wait=2.0,
                                concurrency=1, min_request_interval=0.0),
        )
        running = {
            "status": SimulationStatus.RUNNING, "alpha_id": None, "progress": 0.1,
            "message": None, "retry_after": None,
        }
        runner, _ = make_runner(
            FakeClient(simulation_ids=["remote_t"], status_handler=lambda sid, i: dict(running)),
            store, config,
        )
        timed_out = runner.run_alpha("rank(close)", SETTINGS)
        assert timed_out.status == SimulationStatus.TIMEOUT

        fresh = FakeClient(status_handler=completing_handler)
        runner2, _ = make_runner(fresh, store, config)
        resumed = runner2.resume_incomplete()

        assert len(resumed) == 1
        assert resumed[0].status == SimulationStatus.COMPLETED
        assert fresh.submitted == []
        assert "remote_t" in fresh.polled

    def test_stale_timeout_gets_a_short_recheck_budget(self, store, tmp_path):
        # A row that already burned a full max_wait must not be allowed to stall
        # the run for another full budget: observed live as a 13-minute block
        # before any new candidate could start.
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=5.0, poll_jitter=0.0, max_wait=900.0,
                                concurrency=1, min_request_interval=0.0),
        )
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="stale")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="remote_stale")
        store.update_simulation(
            sim_row, status=SimulationStatus.TIMEOUT, error="gave up earlier",
            mark_completed=True,
        )

        running = {
            "status": SimulationStatus.RUNNING, "alpha_id": None, "progress": 0.1,
            "message": None, "retry_after": None,
        }
        clock = FakeClock()
        client = FakeClient(status_handler=lambda sid, i: dict(running))
        runner, _ = make_runner(client, store, config, clock)

        results = runner.resume_incomplete()

        assert results[0].status == SimulationStatus.TIMEOUT
        # The 30s floor applies here (2 * poll_interval is only 10s), versus the
        # configured 900s — the run is not stalled for another full budget.
        assert clock.total_slept < 60.0
        assert "did not finish within 30s" in results[0].error

    def test_recheck_budget_is_at_least_two_poll_intervals(self, store, tmp_path):
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=20.0, poll_jitter=0.0, max_wait=900.0,
                                concurrency=1, min_request_interval=0.0),
        )
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="stale")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="remote_stale")
        store.update_simulation(sim_row, status=SimulationStatus.TIMEOUT, mark_completed=True)

        running = {
            "status": SimulationStatus.RUNNING, "alpha_id": None, "progress": 0.1,
            "message": None, "retry_after": None,
        }
        client = FakeClient(status_handler=lambda sid, i: dict(running))
        runner, _ = make_runner(client, store, config)
        results = runner.resume_incomplete()

        # 2 * poll_interval beats the 30s floor, so at least one real re-check
        # happens even with a slow poll cadence.
        assert "did not finish within 40s" in results[0].error

    def test_mid_flight_row_keeps_the_full_budget(self, store, tmp_path):
        # A simulation interrupted while RUNNING never got a budget, so it must
        # not be cut short.
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=5.0, poll_jitter=0.0, max_wait=900.0,
                                concurrency=1, min_request_interval=0.0),
        )
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="inflight")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="remote_live")
        store.update_simulation(sim_row, status=SimulationStatus.RUNNING)

        running = {
            "status": SimulationStatus.RUNNING, "alpha_id": None, "progress": 0.1,
            "message": None, "retry_after": None,
        }
        client = FakeClient(status_handler=lambda sid, i: dict(running))
        runner, _ = make_runner(client, store, config)
        results = runner.resume_incomplete()

        assert "did not finish within 900s" in results[0].error

    def test_stale_timeout_that_finished_server_side_is_collected(self, store, tmp_path):
        # The short budget must not cost us a result the server did produce.
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=5.0, poll_jitter=0.0, max_wait=900.0,
                                concurrency=1, min_request_interval=0.0),
        )
        alpha_row = store.upsert_alpha("rank(close)", SETTINGS, name="stale")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="remote_done")
        store.update_simulation(sim_row, status=SimulationStatus.TIMEOUT, mark_completed=True)

        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)
        results = runner.resume_incomplete()

        assert results[0].status == SimulationStatus.COMPLETED
        assert client.submitted == []
        assert "remote_done" in client.polled

    def test_failed_simulation_is_not_auto_resumed(self, store, tmp_path):
        # The server rejected the expression; polling it again cannot help.
        alpha_row = store.upsert_alpha("rank(bad)", SETTINGS, name="bad")
        sim_row = store.create_simulation(alpha_row, remote_simulation_id="remote_bad")
        store.update_simulation(
            sim_row, status=SimulationStatus.FAILED, error="parse error", mark_completed=True
        )

        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(status_handler=completing_handler)
        runner, _ = make_runner(client, store, config)

        assert runner.resume_incomplete() == []
        assert client.polled == []
        assert client.submitted == []


class TestHaltOnAuthFailure:
    def test_auth_error_stops_the_rest_of_the_batch(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(submit_error=AuthError("session expired", status_code=401, url="/simulations"))
        runner, _ = make_runner(client, store, config)

        results = runner.run_batch(["rank(a)", "rank(b)", "rank(c)"])
        assert runner.halted is True
        assert "authentication failure" in runner.halt_reason
        assert results[0].status == SimulationStatus.AUTH_ERROR
        assert all(r.status == SimulationStatus.SKIPPED for r in results[1:])
        assert all(r.passed is False for r in results)

    def test_captcha_never_gets_bypassed_and_halts(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(
            submit_error=CaptchaRequiredError("interactive verification required", url="/simulations")
        )
        runner, _ = make_runner(client, store, config)

        results = runner.run_batch(["rank(a)", "rank(b)"])
        assert runner.halted is True
        assert results[0].status == SimulationStatus.AUTH_ERROR
        assert results[1].status == SimulationStatus.SKIPPED

    def test_halted_run_still_records_what_happened(self, store, tmp_path):
        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(submit_error=AuthError("expired", status_code=401, url="/x"))
        runner, _ = make_runner(client, store, config)
        runner.run_batch(["rank(a)", "rank(b)"])

        counts = store.status_counts()
        assert counts.get(SimulationStatus.AUTH_ERROR) == 1
        rows = store.all_results()
        assert any("expired" in (row.error or "") for row in rows)

    def test_unrelated_api_failure_does_not_halt(self, store, tmp_path):
        from worldquant.exceptions import APIError

        config = make_config(tmp_path, settings=SETTINGS)
        client = FakeClient(submit_error=APIError("boom", status_code=500, url="/simulations"))
        runner, _ = make_runner(client, store, config)
        results = runner.run_batch(["rank(a)", "rank(b)"])

        assert runner.halted is False
        assert all(r.status == SimulationStatus.REQUEST_ERROR for r in results)

    def test_concurrent_halt_still_reports_every_alpha(self, store, tmp_path):
        # Under concurrency the remaining alphas must be reported as SKIPPED,
        # not silently dropped from the results list.
        config = make_config(
            tmp_path, settings=SETTINGS,
            runner=RunnerConfig(poll_interval=1.0, poll_jitter=0.0, max_wait=60.0,
                                concurrency=3, min_request_interval=0.0),
        )
        client = FakeClient(submit_error=AuthError("expired", status_code=401, url="/x"))
        runner, _ = make_runner(client, store, config)

        expressions = [f"rank(f{i})" for i in range(5)]
        results = runner.run_batch(expressions)

        assert len(results) == 5
        assert [r.expression for r in results] == expressions
        assert all(r.status in {SimulationStatus.AUTH_ERROR, SimulationStatus.SKIPPED}
                   for r in results)
        # The skipped results must carry a dedup key consistent with the
        # effective settings, or they could not be matched against the database.
        skipped = [r for r in results if r.status == SimulationStatus.SKIPPED]
        assert all(r.dedup_key == dedup_key(r.expression, SETTINGS) for r in skipped)
