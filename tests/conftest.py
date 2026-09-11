"""Shared test doubles.

Nothing in the test suite touches the network: :class:`FakeSession` replaces
``requests.Session`` for client-level tests, and :class:`FakeClient` replaces
:class:`~worldquant.client.WorldQuantClient` for runner-level tests.
:class:`FakeClock` makes polling and timeouts deterministic and instant.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

import pytest

from worldquant.api import (
    SUBMISSION_CHECKS,
    all_checks_passed,
    failed_check_reasons,
    passed_check_count,
)
from worldquant.config import AppConfig, Credentials, FilterConfig, RetryConfig, RunnerConfig, StorageConfig

#: A realistic ``GET /alphas/{id}/recordsets/yearly-stats`` response, in the
#: format confirmed against the live BRAIN API: a self-describing schema plus
#: positional record rows. Sharpe values match ``test_yearly.FIVE_YEARS`` so
#: stability expectations stay consistent across the suite.
YEARLY_RECORDSET: dict[str, Any] = {
    "schema": {
        "name": "yearly-stats",
        "title": "Yearly Stats",
        "properties": [
            {"name": "year", "title": "Year", "type": "year"},
            {"name": "pnl", "title": "PnL", "type": "amount"},
            {"name": "bookSize", "title": "Book Size", "type": "amount"},
            {"name": "longCount", "title": "Long Count", "type": "integer"},
            {"name": "shortCount", "title": "Short Count", "type": "integer"},
            {"name": "turnover", "title": "Turnover", "type": "percent"},
            {"name": "sharpe", "title": "Sharpe", "type": "decimal"},
            {"name": "returns", "title": "Returns", "type": "percent"},
            {"name": "drawdown", "title": "Drawdown", "type": "percent"},
            {"name": "margin", "title": "Margin", "type": "permyriad"},
            {"name": "fitness", "title": "Fitness", "type": "decimal"},
            {"name": "stage", "title": "Stage", "type": "string"},
        ],
    },
    "records": [
        ["2019", 1100000.0, 20000000, 1551, 1556, 0.40, 1.10, 0.0701, 0.0465, 0.00024, 0.90, "IS"],
        ["2020", -400000.0, 20000000, 1550, 1552, 0.50, -0.40, -0.0337, 0.0377, -0.00105, -0.20, "IS"],
        ["2021", 900000.0, 20000000, 1572, 1581, 0.40, 0.90, 0.0511, 0.0627, 0.00037, 0.70, "IS"],
        ["2022", 1300000.0, 20000000, 1577, 1578, 0.30, 1.30, 0.0617, 0.0593, 0.00041, 1.10, "IS"],
        ["2023", 700000.0, 20000000, 1582, 1585, 0.60, 0.70, 0.0534, 0.0379, 0.00046, 0.50, "IS"],
    ],
}


def submission_checks(results: dict[str, str] | None = None) -> dict[str, Any]:
    """A resolved eight-check submission verdict, all PASS unless overridden.

    ``results`` maps a check name to the verdict it should carry, so a test can
    fail exactly one check and assert the rest still read PASS.
    """
    overrides = dict(results or {})
    return {
        name: {"result": overrides.get(name, "PASS"), "value": None, "limit": None}
        for name in SUBMISSION_CHECKS
    }


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.headers = dict(headers or {})
        self.text = text if text is not None else (
            "" if json_data is None else json.dumps(json_data)
        )
        self.url = "https://api.worldquantbrain.com/test"
        self.reason = "OK" if status_code < 400 else "ERROR"

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    """Records every call and replays queued responses.

    ``responses`` may be a list (consumed in order, last item repeated once
    exhausted) or a callable ``(method, url, kwargs) -> FakeResponse``.
    """

    def __init__(self, responses: Iterable[FakeResponse] | Callable[..., FakeResponse]) -> None:
        self._queue: list[FakeResponse] = (
            list(responses) if not callable(responses) else []
        )
        self._handler = responses if callable(responses) else None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}
        self.auth: Any = None
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if self._handler is not None:
            return self._handler(method, url, kwargs)
        if not self._queue:
            raise AssertionError(f"FakeSession ran out of queued responses for {method} {url}")
        if len(self._queue) == 1:
            return self._queue[0]
        return self._queue.pop(0)

    def close(self) -> None:
        self.closed = True

    # -- assertions helpers -------------------------------------------------
    @property
    def urls(self) -> list[str]:
        return [url for _, url, _ in self.calls]

    def calls_to(self, fragment: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [call for call in self.calls if fragment in call[1]]


class FakeClock:
    """Virtual clock; sleeping advances time instead of blocking."""

    def __init__(self, start: float = 1000.0) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0, f"negative sleep: {seconds}"
        self.sleeps.append(seconds)
        self.current += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


class FakeClient:
    """Scriptable WorldQuantClient double for runner-level tests."""

    def __init__(
        self,
        *,
        simulation_ids: Iterable[str] | None = None,
        status_script: Iterable[dict[str, Any]] | None = None,
        status_handler: Callable[[str, int], dict[str, Any]] | None = None,
        alpha_payloads: dict[str, dict[str, Any]] | None = None,
        yearly_payloads: dict[str, dict[str, dict[str, float | None]]] | None = None,
        submit_error: Exception | None = None,
        poll_error: Exception | None = None,
        check_results: dict[str, dict[str, str]] | None = None,
        check_error: Exception | None = None,
    ) -> None:
        self._simulation_ids = list(simulation_ids or [])
        self._status_script = list(status_script or [])
        self._status_handler = status_handler
        self._alpha_payloads = alpha_payloads or {}
        self._yearly_payloads = yearly_payloads or {}
        self._check_results = dict(check_results or {})
        self.submit_error = submit_error
        self.poll_error = poll_error
        self.check_error = check_error

        self.login_calls = 0
        self.submitted: list[tuple[str, dict[str, Any]]] = []
        self.polled: list[str] = []
        self.poll_counts: dict[str, int] = {}
        self.fetched_alphas: list[str] = []
        self.fetched_yearly: list[str] = []
        self.checked_alphas: list[str] = []
        self.closed = False
        self.is_authenticated = False

    def ensure_authenticated(self) -> None:
        self.login_calls += 1
        self.is_authenticated = True

    def login(self) -> None:
        self.login_calls += 1
        self.is_authenticated = True

    def create_simulation(self, expression: str, settings: dict[str, Any]) -> str:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append((expression, settings))
        if self._simulation_ids:
            return self._simulation_ids.pop(0)
        return f"sim_{len(self.submitted):04d}"

    def get_simulation_status(self, simulation_id: str) -> dict[str, Any]:
        if self.poll_error is not None:
            raise self.poll_error
        self.polled.append(simulation_id)
        poll_index = self.poll_counts.get(simulation_id, 0)
        self.poll_counts[simulation_id] = poll_index + 1

        if self._status_handler is not None:
            entry = dict(self._status_handler(simulation_id, poll_index))
        elif self._status_script:
            entry = dict(self._status_script.pop(0))
        else:
            entry = {
                "status": "COMPLETED",
                "alpha_id": f"alpha_for_{simulation_id}",
                "progress": 1.0,
                "message": None,
                "retry_after": None,
            }
        entry.setdefault("url", f"https://api.worldquantbrain.com/simulations/{simulation_id}")
        return entry

    def get_alpha(self, alpha_id: str) -> dict[str, Any]:
        self.fetched_alphas.append(alpha_id)
        if alpha_id in self._alpha_payloads:
            return self._alpha_payloads[alpha_id]
        return {
            "alpha_id": alpha_id,
            "metrics": {
                "sharpe": 1.5, "fitness": 1.2, "turnover": 0.4, "returns": 0.12,
                "drawdown": 0.05, "margin": 0.0007, "pnl": 1234.5,
                "book_size": 100000.0, "long_count": 1500, "short_count": 1500,
            },
            "settings": {"region": "USA"},
            "checks": {"LOW_SHARPE": {"value": 1.5, "result": "PASS", "limit": 1.25}},
            "date_created": "2026-01-01T00:00:00Z",
            "start_date": "2015-01-01",
            "status": "UNSUBMITTED",
            "raw": {"id": alpha_id, "is": {"sharpe": 1.5}},
        }

    def fetch_yearly_stats(self, alpha_id: str) -> dict[str, dict[str, float | None]]:
        self.fetched_yearly.append(alpha_id)
        return dict(self._yearly_payloads.get(alpha_id, {}))

    def check_submission(self, alpha_id: str) -> dict[str, Any]:
        """Same parsed shape the real client returns, built by the real helpers."""
        self.checked_alphas.append(alpha_id)
        if self.check_error is not None:
            raise self.check_error
        checks = submission_checks(self._check_results.get(alpha_id))
        return {
            "checks": checks,
            "self_correlation": 0.31,
            "self_correlated_with": [],
            "passed_count": passed_check_count(checks),
            "total": len(checks),
            "all_passed": all_checks_passed(checks),
            "failures": failed_check_reasons(checks),
        }

    def close(self) -> None:
        self.closed = True


def make_config(
    tmp_path,
    *,
    filters: FilterConfig | None = None,
    runner: RunnerConfig | None = None,
    retry: RetryConfig | None = None,
    settings: dict[str, Any] | None = None,
    fetch_yearly_stats: bool = False,
) -> AppConfig:
    """Build an AppConfig rooted in a pytest tmp_path, with fast polling."""
    return AppConfig(
        credentials=Credentials(username="tester@example.com", password="s3cret"),
        base_url="https://api.worldquantbrain.com",
        settings=settings or {"region": "USA", "universe": "TOP3000", "delay": 1},
        filters=filters if filters is not None else FilterConfig(
            min_positive_year_ratio=None, max_negative_sharpe_years=None
        ),
        runner=runner if runner is not None else RunnerConfig(
            poll_interval=1.0, poll_jitter=0.0, max_wait=60.0, concurrency=1,
            min_request_interval=0.0,
        ),
        retry=retry if retry is not None else RetryConfig(
            max_retries=3, backoff_base=2.0, backoff_cap=8.0, timeout=5.0, jitter=0.0
        ),
        storage=StorageConfig(
            db_path=tmp_path / "test.db",
            data_dir=tmp_path,
            log_file=tmp_path / "test.log",
            log_level="DEBUG",
        ),
        fetch_yearly_stats=fetch_yearly_stats,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def reset_logging():
    """Undo ``setup_logging``'s global side effects between tests.

    ``setup_logging`` attaches handlers to the shared ``worldquant`` logger and
    sets ``propagate = False``. Left in place, that leaks into later tests and
    breaks ``caplog``, which captures via the root logger.
    """
    import logging

    logger = logging.getLogger("worldquant")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    yield


@pytest.fixture
def store(tmp_path):
    from worldquant.storage import ResultStore

    with ResultStore(tmp_path / "test.db") as result_store:
        yield result_store


def quiet_logger(name: str = "test"):
    """A logger that swallows records, so tests do not spam captured output."""
    import logging

    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def make_runner(client, store, config, clock: FakeClock | None = None):
    """Build a SimulationRunner on a virtual clock. Returns ``(runner, clock)``."""
    from worldquant.simulator import SimulationRunner

    fake_clock = clock or FakeClock()
    runner = SimulationRunner(
        client, store, config, logger=quiet_logger("test.runner"),
        sleep=fake_clock.sleep, clock=fake_clock.now,
    )
    return runner, fake_clock
