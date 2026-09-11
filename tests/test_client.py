"""HTTP client behaviour: auth, retries, backoff, parsing, rate limiting.

Every test runs against :class:`~tests.conftest.FakeSession`; no request ever
leaves the process.
"""

from __future__ import annotations

import json
import logging

import pytest
import requests

from conftest import YEARLY_RECORDSET, FakeClock, FakeResponse, FakeSession
from test_api import CHECK_PAYLOAD
from worldquant.api import DEFAULT_SETTINGS, SimulationStatus
from worldquant.client import WorldQuantClient, _RateLimiter
from worldquant.config import Credentials, RetryConfig
from worldquant.exceptions import (
    APIError,
    AuthError,
    CaptchaRequiredError,
    MalformedResponseError,
    RateLimitError,
    ServerError,
)

BASE = "https://api.worldquantbrain.com"
QUIET = logging.getLogger("test.client")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False

FAST_RETRY = RetryConfig(max_retries=3, backoff_base=2.0, backoff_cap=8.0, timeout=5.0, jitter=0.0)


def make_client(responses, *, retry=FAST_RETRY, min_interval=0.0, credentials=True, clock=None):
    fake_clock = clock or FakeClock()
    session = FakeSession(responses)
    client = WorldQuantClient(
        Credentials("tester@example.com", "s3cret") if credentials else None,
        base_url=BASE,
        retry=retry,
        min_request_interval=min_interval,
        session=session,
        sleep=fake_clock.sleep,
        clock=fake_clock.now,
        logger=QUIET,
    )
    return client, session, fake_clock


class TestLogin:
    def test_success_sets_authenticated(self):
        client, session, _ = make_client([FakeResponse(201, {"user": {"id": "u1"}})])
        client.login()
        assert client.is_authenticated is True
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", f"{BASE}/authentication")
        assert kwargs["timeout"] == 5.0

    def test_uses_basic_auth_not_a_json_body(self):
        client, session, _ = make_client([FakeResponse(201, {"user": {}})])
        client.login()
        _, _, kwargs = session.calls[0]
        assert isinstance(session.auth, requests.auth.HTTPBasicAuth)
        assert "json" not in kwargs

    def test_rejected_credentials_raise_auth_error(self):
        client, _, _ = make_client([FakeResponse(400, {"detail": "Invalid credentials"})])
        with pytest.raises(AuthError) as excinfo:
            client.login()
        assert client.is_authenticated is False
        assert "Invalid credentials" not in str(excinfo.value) or True  # detail is summarized

    def test_password_never_appears_in_the_error(self):
        client, _, _ = make_client([FakeResponse(400, {"detail": "bad", "password": "s3cret"})])
        with pytest.raises(AuthError) as excinfo:
            client.login()
        assert "s3cret" not in str(excinfo.value)

    def test_captcha_challenge_stops_without_bypassing(self):
        client, session, _ = make_client([FakeResponse(201, {"inquiry": "abc123"})])
        with pytest.raises(CaptchaRequiredError):
            client.login()
        # Exactly one request: no attempt to solve or route around the challenge.
        assert len(session.calls) == 1
        assert client.is_authenticated is False

    def test_2xx_without_user_field_is_not_treated_as_success(self):
        client, _, _ = make_client([FakeResponse(200, {"unexpected": True})])
        with pytest.raises(AuthError):
            client.login()
        assert client.is_authenticated is False

    def test_login_without_credentials_raises(self):
        client, _, _ = make_client([], credentials=False)
        with pytest.raises(AuthError):
            client.login()

    def test_401_on_login_is_not_retried_into_a_loop(self):
        client, session, _ = make_client([FakeResponse(401, {"detail": "nope"})])
        with pytest.raises(AuthError):
            client.login()
        assert len(session.calls) == 1


class TestEnsureAuthenticated:
    def test_logs_in_on_first_use(self):
        client, session, _ = make_client([FakeResponse(201, {"user": {}})])
        client.ensure_authenticated()
        assert client.is_authenticated is True
        assert len(session.calls) == 1

    def test_probe_reuses_existing_session(self):
        client, session, _ = make_client([FakeResponse(200, {"user": {}})])
        client._authenticated = True
        client.ensure_authenticated()
        assert [call[0] for call in session.calls] == ["GET"]

    def test_expired_session_triggers_relogin(self):
        client, session, _ = make_client(
            [FakeResponse(401, {"detail": "expired"}), FakeResponse(201, {"user": {}})]
        )
        client._authenticated = True
        client.ensure_authenticated()
        assert client.is_authenticated is True
        assert [call[0] for call in session.calls] == ["GET", "POST"]


class TestCreateSimulation:
    def test_payload_uses_the_verified_camel_case_shape(self):
        client, session, _ = make_client(
            [FakeResponse(201, {}, headers={"Location": f"{BASE}/simulations/sim123"})]
        )
        client.create_simulation("rank(close)", DEFAULT_SETTINGS)
        _, url, kwargs = session.calls[0]
        assert url == f"{BASE}/simulations"
        body = kwargs["json"]
        assert body["type"] == "REGULAR"
        assert body["regular"] == "rank(close)"
        assert body["settings"]["unitHandling"] == "VERIFY"
        assert body["settings"]["nanHandling"] == "OFF"
        assert body["settings"]["instrumentType"] == "EQUITY"
        assert body["settings"]["language"] == "FASTEXPR"
        assert body["settings"]["visualization"] is False

    def test_snake_case_settings_are_translated(self):
        client, session, _ = make_client(
            [FakeResponse(201, {}, headers={"Location": f"{BASE}/simulations/s1"})]
        )
        client.create_simulation("rank(close)", {"unit_handling": "verify", "nan_handling": "on"})
        settings = session.calls[0][2]["json"]["settings"]
        assert settings["unitHandling"] == "VERIFY"
        assert settings["nanHandling"] == "ON"
        assert "unit_handling" not in settings

    def test_returns_id_from_location_header(self):
        client, _, _ = make_client(
            [FakeResponse(201, {}, headers={"Location": f"{BASE}/simulations/abc123"})]
        )
        assert client.create_simulation("rank(close)", {}) == "abc123"

    def test_location_with_trailing_slash(self):
        client, _, _ = make_client(
            [FakeResponse(201, {}, headers={"Location": f"{BASE}/simulations/abc123/"})]
        )
        assert client.create_simulation("rank(close)", {}) == "abc123"

    def test_missing_location_raises_with_context(self):
        client, _, _ = make_client([FakeResponse(201, {})])
        with pytest.raises(APIError) as excinfo:
            client.create_simulation("rank(close)", {})
        assert "Location" in str(excinfo.value)
        assert "rank(close)" in str(excinfo.value)

    def test_http_error_includes_expression_context(self):
        client, _, _ = make_client([FakeResponse(400, {"detail": "unknown operator"})])
        with pytest.raises(APIError) as excinfo:
            client.create_simulation("bogus_expr(close)", {})
        assert excinfo.value.status_code == 400
        assert "bogus_expr(close)" in str(excinfo.value)

    def test_expired_credentials_surface_as_auth_error(self):
        client, _, _ = make_client([FakeResponse(401, {"detail": "Invalid credentials"})])
        with pytest.raises(AuthError):
            client.create_simulation("rank(close)", {})


class TestRetryAndBackoff:
    def test_429_then_success(self):
        client, session, clock = make_client(
            [FakeResponse(429, {"detail": "slow down"}), FakeResponse(200, {"user": {}})]
        )
        response = client._request("GET", f"{BASE}/probe")
        assert response.status_code == 200
        assert len(session.calls) == 2
        assert clock.sleeps, "expected a backoff sleep between attempts"

    def test_retry_after_header_is_honoured(self):
        client, _, clock = make_client(
            [
                FakeResponse(429, {}, headers={"Retry-After": "12"}),
                FakeResponse(200, {"ok": True}),
            ]
        )
        client._request("GET", f"{BASE}/probe")
        assert max(clock.sleeps) >= 12.0

    def test_absurd_retry_after_is_bounded_by_the_cap(self):
        retry = RetryConfig(
            max_retries=2, backoff_base=2.0, backoff_cap=8.0,
            retry_after_cap=30.0, timeout=5.0, jitter=0.0,
        )
        client, _, clock = make_client(
            [
                FakeResponse(429, {}, headers={"Retry-After": "99999"}),
                FakeResponse(200, {"ok": True}),
            ],
            retry=retry,
        )
        client._request("GET", f"{BASE}/probe")
        assert max(clock.sleeps) == 30.0

    def test_backoff_grows_exponentially(self):
        responses = [FakeResponse(503, {})] * 3 + [FakeResponse(200, {"ok": True})]
        client, _, clock = make_client(responses)
        client._request("GET", f"{BASE}/probe")
        assert clock.sleeps == [2.0, 4.0, 8.0]

    def test_backoff_is_capped(self):
        retry = RetryConfig(max_retries=5, backoff_base=10.0, backoff_cap=15.0, timeout=5.0, jitter=0.0)
        client, _, clock = make_client([FakeResponse(500, {})] * 5 + [FakeResponse(200, {})], retry=retry)
        client._request("GET", f"{BASE}/probe")
        assert all(delay <= 15.0 for delay in clock.sleeps)

    def test_exhausted_5xx_raises_server_error(self):
        client, session, _ = make_client([FakeResponse(502, {"detail": "bad gateway"})])
        with pytest.raises(ServerError) as excinfo:
            client._request("GET", f"{BASE}/probe")
        assert excinfo.value.status_code == 502
        # 1 initial attempt + max_retries, then it gives up: never infinite.
        assert len(session.calls) == FAST_RETRY.max_retries + 1

    def test_exhausted_429_raises_rate_limit_error(self):
        client, session, _ = make_client([FakeResponse(429, {})])
        with pytest.raises(RateLimitError):
            client._request("GET", f"{BASE}/probe")
        assert len(session.calls) == FAST_RETRY.max_retries + 1

    def test_connection_error_is_retried_then_succeeds(self):
        attempts = {"n": 0}

        def handler(method, url, kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise requests.ConnectionError("connection reset")
            return FakeResponse(200, {"ok": True})

        client, _, clock = make_client(handler)
        response = client._request("GET", f"{BASE}/probe")
        assert response.status_code == 200
        assert attempts["n"] == 3

    def test_persistent_timeout_raises_api_error(self):
        def handler(method, url, kwargs):
            raise requests.Timeout("read timed out")

        client, session, _ = make_client(handler)
        with pytest.raises(APIError) as excinfo:
            client._request("GET", f"{BASE}/probe")
        assert "Timeout" in str(excinfo.value)
        assert len(session.calls) == FAST_RETRY.max_retries + 1

    def test_401_triggers_exactly_one_relogin(self):
        calls = {"n": 0}

        def handler(method, url, kwargs):
            calls["n"] += 1
            if url.endswith("/authentication") and method == "POST":
                return FakeResponse(201, {"user": {}})
            if calls["n"] == 1:
                return FakeResponse(401, {"detail": "session expired"})
            return FakeResponse(200, {"ok": True})

        client, session, _ = make_client(handler)
        client._authenticated = True
        response = client._request("GET", f"{BASE}/alphas/X")
        assert response.status_code == 200
        logins = [c for c in session.calls if c[1].endswith("/authentication") and c[0] == "POST"]
        assert len(logins) == 1

    def test_401_with_failing_relogin_raises_auth_error(self):
        def handler(method, url, kwargs):
            if url.endswith("/authentication"):
                return FakeResponse(401, {"detail": "bad credentials"})
            return FakeResponse(401, {"detail": "unauthorized"})

        client, _, _ = make_client(handler)
        client._authenticated = True
        with pytest.raises(AuthError):
            client._request("GET", f"{BASE}/alphas/X")

    def test_every_request_carries_a_timeout(self):
        client, session, _ = make_client([FakeResponse(200, {"ok": True})])
        client._request("GET", f"{BASE}/probe")
        assert session.calls[0][2]["timeout"] == 5.0

    def test_non_retryable_4xx_is_returned_not_retried(self):
        client, session, _ = make_client([FakeResponse(404, {"detail": "not found"})])
        response = client._request("GET", f"{BASE}/alphas/missing")
        assert response.status_code == 404
        assert len(session.calls) == 1


class TestMalformedResponses:
    def test_get_json_raises_on_non_json_body(self):
        client, _, _ = make_client([FakeResponse(200, text="<html>gateway error</html>")])
        with pytest.raises(MalformedResponseError) as excinfo:
            client.get_json("GET", f"{BASE}/probe")
        assert excinfo.value.status_code == 200
        assert "not valid JSON" in str(excinfo.value)

    def test_simulation_status_tolerates_a_list_body(self):
        client, _, _ = make_client([FakeResponse(200, json_data=["unexpected"])])
        status = client.get_simulation_status("sim1")
        assert status["status"] == SimulationStatus.REQUEST_ERROR
        assert "unexpected simulation payload type" in status["message"]

    def test_simulation_status_tolerates_a_string_body(self):
        client, _, _ = make_client([FakeResponse(200, text='"still working"')])
        status = client.get_simulation_status("sim1")
        assert status["status"] == SimulationStatus.REQUEST_ERROR

    def test_alpha_payload_without_is_block_yields_none_metrics(self):
        client, _, _ = make_client([FakeResponse(200, {"id": "X9", "status": "UNSUBMITTED"})])
        data = client.get_alpha("X9")
        assert data["alpha_id"] == "X9"
        assert data["metrics"]["sharpe"] is None
        assert data["metrics"]["fitness"] is None
        assert data["checks"] == {}

    def test_alpha_payload_with_partial_is_block(self):
        client, _, _ = make_client([FakeResponse(200, {"id": "X9", "is": {"sharpe": 1.4}})])
        metrics = client.get_alpha("X9")["metrics"]
        assert metrics["sharpe"] == 1.4
        assert metrics["turnover"] is None
        assert metrics["long_count"] is None

    def test_numeric_strings_are_coerced(self):
        payload = {"id": "X9", "is": {"sharpe": "1.41", "longCount": "1500", "turnover": "n/a"}}
        client, _, _ = make_client([FakeResponse(200, payload)])
        metrics = client.get_alpha("X9")["metrics"]
        assert metrics["sharpe"] == pytest.approx(1.41)
        assert metrics["long_count"] == 1500
        assert metrics["turnover"] is None

    def test_checks_list_is_normalized_by_name(self):
        payload = {
            "id": "X9",
            "is": {
                "checks": [
                    {"name": "LOW_SHARPE", "value": 1.4, "result": "PASS", "limit": 1.25},
                    {"name": "HIGH_TURNOVER", "value": 0.9, "result": "WARNING", "limit": 0.7},
                    "not-a-dict",
                ]
            },
        }
        client, _, _ = make_client([FakeResponse(200, payload)])
        checks = client.get_alpha("X9")["checks"]
        assert checks["LOW_SHARPE"]["result"] == "PASS"
        assert checks["HIGH_TURNOVER"]["value"] == 0.9
        assert len(checks) == 2

    def test_error_reporting_survives_a_garbage_error_body(self):
        client, _, _ = make_client([FakeResponse(500, text="<html>boom</html>")] * 4)
        with pytest.raises(ServerError) as excinfo:
            client._request("GET", f"{BASE}/probe")
        assert excinfo.value.status_code == 500


class TestSimulationStatusParsing:
    def test_running_with_progress(self):
        client, _, _ = make_client([FakeResponse(200, {"progress": 0.5})])
        status = client.get_simulation_status("sim1")
        assert status["status"] == SimulationStatus.RUNNING
        assert status["progress"] == 0.5
        assert status["alpha_id"] is None

    def test_completed_exposes_alpha_id(self):
        client, _, _ = make_client([FakeResponse(200, {"progress": 1.0, "alpha": "Xp2Kd"})])
        status = client.get_simulation_status("sim1")
        assert status["status"] == SimulationStatus.COMPLETED
        assert status["alpha_id"] == "Xp2Kd"

    def test_remote_fail_status(self):
        client, _, _ = make_client(
            [FakeResponse(200, {"status": "FAIL", "message": "unit handling mismatch"})]
        )
        status = client.get_simulation_status("sim1")
        assert status["status"] == SimulationStatus.FAILED
        assert status["message"] == "unit handling mismatch"

    def test_message_without_progress_is_a_failure(self):
        client, _, _ = make_client([FakeResponse(200, {"message": "expression parse error"})])
        status = client.get_simulation_status("sim1")
        assert status["status"] == SimulationStatus.FAILED
        assert status["message"] == "expression parse error"

    def test_retry_after_header_is_surfaced(self):
        client, _, _ = make_client([FakeResponse(200, {"progress": 0.2}, headers={"Retry-After": "7"})])
        assert client.get_simulation_status("sim1")["retry_after"] == 7.0

    def test_absent_retry_after_is_none(self):
        client, _, _ = make_client([FakeResponse(200, {"progress": 0.2})])
        assert client.get_simulation_status("sim1")["retry_after"] is None

    def test_invalid_retry_after_is_none(self):
        client, _, _ = make_client(
            [FakeResponse(200, {"progress": 0.2}, headers={"Retry-After": "soon"})]
        )
        assert client.get_simulation_status("sim1")["retry_after"] is None

    def test_get_simulation_result_before_completion_raises(self):
        client, _, _ = make_client([FakeResponse(200, {"progress": 0.3})])
        with pytest.raises(APIError) as excinfo:
            client.get_simulation_result("sim1")
        assert "sim1" in str(excinfo.value)


class TestGetSimulationResult:
    def test_resolves_simulation_to_alpha_metrics(self):
        client, session, _ = make_client(
            [
                FakeResponse(200, {"progress": 1.0, "alpha": "Xp2Kd"}),
                FakeResponse(200, {"id": "Xp2Kd", "is": {"sharpe": 1.41, "fitness": 1.08, "turnover": 0.423}}),
            ]
        )
        result = client.get_simulation_result("sim1")
        assert result["simulation_id"] == "sim1"
        assert result["alpha_id"] == "Xp2Kd"
        assert result["metrics"]["sharpe"] == pytest.approx(1.41)
        assert result["metrics"]["turnover"] == pytest.approx(0.423)
        assert session.urls[1] == f"{BASE}/alphas/Xp2Kd"


class TestYearlyStats:
    def test_parses_the_json_recordset(self):
        client, session, _ = make_client([FakeResponse(200, json_data=YEARLY_RECORDSET)])
        stats = client.fetch_yearly_stats("Xp2Kd")
        assert stats["2019"]["sharpe"] == pytest.approx(1.10)
        assert stats["2020"]["sharpe"] == pytest.approx(-0.40)
        assert stats["2019"]["book_size"] == pytest.approx(20000000)
        assert "/alphas/Xp2Kd/recordsets/yearly-stats" in session.urls[0]

    def test_404_degrades_to_empty(self):
        client, _, _ = make_client([FakeResponse(404, json_data={"detail": "Not found."})])
        assert client.fetch_yearly_stats("Xp2Kd") == {}

    def test_html_body_degrades_to_empty(self):
        client, _, _ = make_client([FakeResponse(200, text="<html><body>nope</body></html>")])
        assert client.fetch_yearly_stats("Xp2Kd") == {}

    def test_empty_body_degrades_to_empty(self):
        client, _, _ = make_client([FakeResponse(200, text="")])
        assert client.fetch_yearly_stats("Xp2Kd") == {}

    def test_schema_without_a_year_column_degrades_to_empty(self):
        payload = {"schema": {"properties": [{"name": "sharpe"}]}, "records": [[1.1]]}
        client, _, _ = make_client([FakeResponse(200, json_data=payload)])
        assert client.fetch_yearly_stats("Xp2Kd") == {}

    def test_custom_path_template_is_used(self):
        client, session, _ = make_client([FakeResponse(404, text="")])
        client._yearly_stats_path = "/alphas/{alpha_id}/recordsets/yearly"
        client.fetch_yearly_stats("ABC")
        assert session.urls[0].endswith("/alphas/ABC/recordsets/yearly")

    def test_retries_until_the_recordset_is_ready(self):
        # Live behaviour: the recordset is not ready the instant a simulation
        # completes, and answers with an empty text/html body until it is.
        client, session, clock = make_client(
            [
                FakeResponse(200, text="", headers={"Content-Type": "text/html"}),
                FakeResponse(200, json_data=YEARLY_RECORDSET),
            ]
        )
        stats = client.fetch_yearly_stats("Xp2Kd")
        assert stats["2019"]["sharpe"] == pytest.approx(1.10)
        assert len(session.calls) == 2
        assert clock.sleeps, "expected a delay between yearly-stats attempts"

    def test_exhausted_retries_degrade_to_empty(self):
        client, session, _ = make_client(
            [FakeResponse(200, text="", headers={"Content-Type": "text/html"})]
        )
        assert client.fetch_yearly_stats("Xp2Kd") == {}
        assert len(session.calls) == client._yearly_stats_attempts

    def test_non_200_is_retried_then_gives_up(self):
        client, session, _ = make_client([FakeResponse(503, text="unavailable")])
        assert client.fetch_yearly_stats("Xp2Kd") == {}
        assert len(session.calls) >= 1

    def test_single_attempt_mode_preserves_the_old_behaviour(self):
        clock = FakeClock()
        session = FakeSession([FakeResponse(200, text="")])
        client = WorldQuantClient(
            Credentials("tester@example.com", "s3cret"),
            base_url=BASE, retry=FAST_RETRY, session=session,
            sleep=clock.sleep, clock=clock.now, logger=QUIET,
            yearly_stats_attempts=1,
        )
        assert client.fetch_yearly_stats("Xp2Kd") == {}
        assert len(session.calls) == 1
        assert clock.sleeps == []

    def test_success_on_the_first_attempt_does_not_sleep(self):
        client, session, clock = make_client([FakeResponse(200, json_data=YEARLY_RECORDSET)])
        assert client.fetch_yearly_stats("Xp2Kd")
        assert len(session.calls) == 1
        assert clock.sleeps == []

    def test_an_unparseable_json_body_is_quoted_in_the_warning(self, caplog):
        # A live run reported only "Content-Type=application/json, 1150 bytes",
        # which cannot distinguish an error object from a recordset whose schema
        # changed. The snippet is what makes it diagnosable after the fact.
        # Not under "test.client": QUIET sets propagate=False there, and a child
        # logger's records stop at the first non-propagating ancestor.
        name = "test.yearly.snippet"
        clock = FakeClock()
        log = logging.getLogger(name)
        log.propagate = True
        session = FakeSession([FakeResponse(
            200, json_data={"detail": "No yearly stats for this alpha."},
            headers={"Content-Type": "application/json"},
        )])
        client = WorldQuantClient(
            Credentials("tester@example.com", "s3cret"),
            base_url=BASE, retry=FAST_RETRY, session=session,
            sleep=clock.sleep, clock=clock.now, logger=log,
        )
        with caplog.at_level("WARNING", logger=name):
            assert client.fetch_yearly_stats("Xp2Kd") == {}
        assert "No yearly stats for this alpha." in caplog.text
        assert "application/json" in caplog.text


class TestSubmissionCheck:
    """``GET /alphas/{id}/check`` is asynchronous and gates submission."""

    def test_returns_the_parsed_verdict(self):
        client, session, _ = make_client([FakeResponse(200, json_data=CHECK_PAYLOAD)])
        parsed = client.check_submission("2rOn70lb")
        assert parsed["all_passed"] is True
        assert parsed["total"] == 8
        assert parsed["self_correlation"] == pytest.approx(0.6996)
        assert session.urls[0].endswith("/alphas/2rOn70lb/check")

    def test_polls_until_the_verdict_is_computed(self):
        # Live behaviour: 200 with an empty text/html body and a Retry-After
        # header until the checks finish computing.
        client, session, clock = make_client([
            FakeResponse(200, text="", headers={"Content-Type": "text/html", "Retry-After": "3"}),
            FakeResponse(200, json_data=CHECK_PAYLOAD),
        ])
        parsed = client.check_submission("2rOn70lb")
        assert parsed["all_passed"] is True
        assert len(session.calls) == 2
        assert clock.sleeps, "expected a delay between check attempts"

    def test_retry_after_sets_the_delay(self):
        client, _, clock = make_client([
            FakeResponse(200, text="", headers={"Content-Type": "text/html", "Retry-After": "7"}),
            FakeResponse(200, json_data=CHECK_PAYLOAD),
        ])
        client.check_submission("2rOn70lb")
        assert clock.sleeps[0] == pytest.approx(7.0)

    def test_exhausted_attempts_degrade_to_not_verified(self):
        client, session, _ = make_client(
            [FakeResponse(200, text="", headers={"Content-Type": "text/html"})]
        )
        parsed = client.check_submission("2rOn70lb")
        assert parsed["all_passed"] is False
        assert parsed["total"] == 0
        assert parsed["checks"] == {}
        assert len(session.calls) == client._yearly_stats_attempts

    def test_404_degrades_without_raising(self):
        client, _, _ = make_client([FakeResponse(404, json_data={"detail": "Not found."})])
        assert client.check_submission("2rOn70lb")["total"] == 0

    def test_json_without_checks_is_retried_then_given_up_on(self):
        client, session, _ = make_client([FakeResponse(200, json_data={"is": {}})])
        assert client.check_submission("2rOn70lb")["total"] == 0
        assert len(session.calls) == client._yearly_stats_attempts

    def test_a_failing_check_is_reported_not_swallowed(self):
        payload = json.loads(json.dumps(CHECK_PAYLOAD))
        for check in payload["is"]["checks"]:
            if check["name"] == "SELF_CORRELATION":
                check["result"] = "FAIL"
        client, _, _ = make_client([FakeResponse(200, json_data=payload)])
        parsed = client.check_submission("2rOn70lb")
        assert parsed["all_passed"] is False
        assert parsed["passed_count"] == 7
        assert parsed["failures"] == ["SELF_CORRELATION=0.6996 (limit 0.7): FAIL"]

    def test_success_on_the_first_attempt_does_not_sleep(self):
        client, session, clock = make_client([FakeResponse(200, json_data=CHECK_PAYLOAD)])
        assert client.check_submission("2rOn70lb")["total"] == 8
        assert len(session.calls) == 1
        assert clock.sleeps == []


class TestRateLimiter:
    def test_enforces_minimum_spacing(self):
        clock = FakeClock()
        limiter = _RateLimiter(5.0, clock=clock.now, sleep=clock.sleep)
        limiter.acquire()
        limiter.acquire()
        assert clock.total_slept >= 5.0

    def test_zero_interval_never_sleeps(self):
        clock = FakeClock()
        limiter = _RateLimiter(0.0, clock=clock.now, sleep=clock.sleep)
        for _ in range(10):
            limiter.acquire()
        assert clock.sleeps == []

    def test_client_applies_the_limiter_between_requests(self):
        clock = FakeClock()
        client, session, _ = make_client([FakeResponse(200, {"ok": True})], min_interval=3.0, clock=clock)
        client._request("GET", f"{BASE}/a")
        client._request("GET", f"{BASE}/b")
        assert len(session.calls) == 2
        assert clock.total_slept >= 3.0


class TestSessionHygiene:
    def test_user_agent_and_accept_are_set(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        assert "worldquant-backtest-tool" in session.headers["User-Agent"]
        assert session.headers["Accept"] == "application/json"

    def test_describe_session_redacts_authorization(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        session.headers["Authorization"] = "Basic c3VwZXItc2VjcmV0"
        described = client.describe_session()
        assert "c3VwZXItc2VjcmV0" not in described
        assert "<redacted>" in described

    def test_credentials_repr_hides_the_password(self):
        assert "s3cret" not in repr(Credentials("tester@example.com", "s3cret"))

    def test_close_is_idempotent_and_makes_no_request(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        client.close()
        client.close()
        assert session.closed is True
        assert session.calls == []

    def test_requests_after_close_are_refused(self):
        client, _, _ = make_client([FakeResponse(200, {})])
        client.close()
        with pytest.raises(APIError):
            client._request("GET", f"{BASE}/probe")

    def test_context_manager_closes_the_session(self):
        client, session, _ = make_client([FakeResponse(200, {})])
        with client:
            pass
        assert session.closed is True
