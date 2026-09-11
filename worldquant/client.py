"""HTTP client for the WorldQuant BRAIN backend.

Every network call in this project goes through :meth:`WorldQuantClient._request`,
which owns timeouts, retries, rate limiting, 429/5xx backoff, credential-expiry
recovery and log redaction. Endpoint URLs and payload shapes live in
:mod:`worldquant.api`, not here.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

import requests
from requests.auth import HTTPBasicAuth

from . import api
from .config import Credentials, RetryConfig
from .exceptions import (
    APIError,
    AuthError,
    CaptchaRequiredError,
    MalformedResponseError,
    RateLimitError,
    ServerError,
    summarize_payload,
)
from .logging_utils import get_logger, redact_headers

#: HTTP statuses that are worth retrying with backoff.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_UNAUTHORIZED_STATUS = frozenset({401, 403})

#: The yearly-stats recordset lags behind simulation completion, so it is fetched
#: with a few spaced attempts rather than exactly once. See fetch_yearly_stats.
YEARLY_STATS_ATTEMPTS = 3
YEARLY_STATS_DELAY = 5.0


class _RateLimiter:
    """Enforces a minimum spacing between requests across all threads.

    A global floor (rather than per-thread) is what actually keeps a concurrent
    batch from bursting many requests per second at the BRAIN backend.
    """

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._next_allowed = now + self._min_interval

    @property
    def min_interval(self) -> float:
        return self._min_interval


class WorldQuantClient:
    """Thin, retrying wrapper over the BRAIN REST endpoints.

    Args:
        credentials: Email/password. Kept in memory only; never logged or stored.
        base_url: API root, defaults to ``https://api.worldquantbrain.com``.
        retry: Backoff/timeout policy.
        min_request_interval: Global floor between any two HTTP requests.
        session: Injectable ``requests.Session`` (used by the test suite).
        sleep: Injectable sleep function (used by the test suite).
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        base_url: str = api.DEFAULT_BASE_URL,
        retry: RetryConfig | None = None,
        min_request_interval: float = 1.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        user_agent: str = api.DEFAULT_USER_AGENT,
        yearly_stats_path: str = api.DEFAULT_YEARLY_STATS_PATH,
        yearly_stats_attempts: int = YEARLY_STATS_ATTEMPTS,
        yearly_stats_delay: float = YEARLY_STATS_DELAY,
        logger: Any = None,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.retry = retry or RetryConfig()
        self._sleep = sleep
        self._clock = clock
        self._yearly_stats_path = yearly_stats_path
        self._yearly_stats_attempts = max(1, int(yearly_stats_attempts))
        self._yearly_stats_delay = max(0.0, float(yearly_stats_delay))
        self.log = logger or get_logger("client")

        self._rate_limiter = _RateLimiter(min_request_interval, clock=clock, sleep=sleep)
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
            }
        )
        if credentials is not None:
            self._session.auth = HTTPBasicAuth(credentials.username, credentials.password)

        self._authenticated = False
        self._login_generation = 0
        self._relogin_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "WorldQuantClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool. Idempotent.

        Deliberately does not call ``DELETE /authentication``: logging out would
        invalidate the session for any other process using the same account.
        Call :meth:`logout` explicitly when you want that.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._session.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            self.log.debug("session close failed: %s", exc)

    def logout(self) -> None:
        """Best-effort server-side logout."""
        try:
            self._request("DELETE", api.authentication_url(self.base_url))
        except APIError as exc:
            self.log.warning("logout failed: %s", exc)
        finally:
            self._authenticated = False

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def login(self) -> None:
        """Establish a session via ``POST /authentication``.

        Verified contract: HTTP Basic Auth, ``201`` on success, body contains a
        ``user`` object. A body containing ``inquiry`` means the platform wants
        an interactive biometric/persona challenge — we stop rather than attempt
        to work around it.
        """
        if self.credentials is None:
            raise AuthError("cannot login: no credentials configured", url=self.base_url)

        url = api.authentication_url(self.base_url)
        self.log.info("Logging in as %s", self.credentials.username)
        self._authenticated = False

        try:
            response = self._request("POST", url, allow_relogin=False)
        except AuthError:
            raise
        except APIError as exc:
            raise AuthError(
                "login request failed",
                status_code=exc.status_code,
                url=url,
                detail=exc.detail,
            ) from exc

        payload = self._safe_json(response)
        if api.is_captcha_payload(payload):
            raise CaptchaRequiredError(
                "BRAIN requires an interactive verification challenge (biometric/persona). "
                "Complete it manually in a browser at platform.worldquantbrain.com, then re-run. "
                "This tool will not bypass platform verification.",
                status_code=response.status_code,
                url=url,
                detail=summarize_payload(payload),
            )

        if response.status_code not in (200, 201):
            raise AuthError(
                "login rejected",
                status_code=response.status_code,
                url=url,
                detail=summarize_payload(payload),
            )

        if not isinstance(payload, dict) or "user" not in payload:
            # A 2xx without a `user` block is unexpected; do not pretend we are
            # logged in, because every later call would fail confusingly.
            raise AuthError(
                "login response did not contain the expected 'user' field",
                status_code=response.status_code,
                url=url,
                detail=summarize_payload(payload),
            )

        self._authenticated = True
        self.log.info("Login successful")

    def ensure_authenticated(self) -> None:
        """Login when needed, and verify a previously established session.

        Uses ``GET /authentication`` as the cheapest session probe.
        """
        if not self._authenticated:
            self.login()
            return

        url = api.authentication_url(self.base_url)
        try:
            response = self._request("GET", url, allow_relogin=False)
        except AuthError:
            self._authenticated = False
            self.log.info("Session expired; logging in again")
            self.login()
            return

        if response.status_code in _UNAUTHORIZED_STATUS:
            self._authenticated = False
            self.log.info("Session no longer valid (HTTP %d); logging in again", response.status_code)
            self.login()

    # ------------------------------------------------------------------ #
    # Simulations
    # ------------------------------------------------------------------ #
    def create_simulation(self, expression: str, settings: dict[str, Any]) -> str:
        """Submit one alpha and return its simulation id.

        Verified contract: ``POST /simulations`` with
        ``{"type": "REGULAR", "regular": expr, "settings": {...}}`` answers
        ``201`` and exposes the simulation URL in the ``Location`` header.
        """
        url = api.simulations_url(self.base_url)
        payload = api.build_simulation_payload(expression, settings)
        response = self._request("POST", url, json=payload)

        if response.status_code not in (200, 201, 202):
            body = self._safe_json(response)
            if api.is_credentials_payload(body):
                raise AuthError(
                    "simulation rejected: credentials no longer valid",
                    status_code=response.status_code,
                    url=url,
                    detail=summarize_payload(body),
                )
            raise APIError(
                f"could not create simulation for expression {expression!r}",
                status_code=response.status_code,
                url=url,
                detail=summarize_payload(body),
            )

        location = response.headers.get(api.LOCATION_HEADER)
        if not location:
            # Seen in the wild when BRAIN is saturated: 201 without a Location.
            # Treat it as retryable rather than as a silent failure.
            raise APIError(
                f"simulation response had no {api.LOCATION_HEADER} header "
                f"(expression={expression!r}); the backend may be saturated",
                status_code=response.status_code,
                url=url,
                detail=summarize_payload(self._safe_json(response)),
            )

        simulation_id = api.simulation_id_from_location(location)
        if not simulation_id:
            raise MalformedResponseError(
                f"could not derive a simulation id from Location={location!r}",
                status_code=response.status_code,
                url=url,
            )
        return simulation_id

    def get_simulation_status(self, simulation_id: str) -> dict[str, Any]:
        """Poll ``GET /simulations/{id}`` once.

        Returns a dict with ``status`` (internal), ``alpha_id``, ``progress``,
        ``message``, ``retry_after`` (server-suggested poll delay, may be None)
        and ``raw`` (the untouched body).
        """
        url = api.simulation_url(self.base_url, simulation_id)
        response = self._request("GET", url)
        payload = self._safe_json(response)
        parsed = api.parse_simulation_progress(payload)
        parsed["retry_after"] = _parse_retry_after(response.headers.get(api.RETRY_AFTER_HEADER))
        parsed["status_code"] = response.status_code
        parsed["raw"] = payload
        parsed["url"] = url
        return parsed

    def get_alpha(self, alpha_id: str) -> dict[str, Any]:
        """Fetch and parse ``GET /alphas/{alpha_id}``.

        Returns ``{"alpha_id", "metrics", "settings", "checks", "date_created",
        "status", "raw"}``. Every metric is ``None`` when BRAIN omits it.
        """
        url = api.alpha_url(self.base_url, alpha_id)
        response = self._request("GET", url)
        payload = self._safe_json(response)
        parsed = api.parse_alpha_payload(payload)
        return {
            "alpha_id": parsed.get("alpha_id") or alpha_id,
            "metrics": {
                key: parsed.get(key)
                for key in (
                    "sharpe", "fitness", "turnover", "returns", "drawdown",
                    "margin", "pnl", "book_size", "long_count", "short_count",
                )
            },
            "settings": parsed.get("settings"),
            "checks": parsed.get("checks") or {},
            "date_created": parsed.get("date_created"),
            "start_date": parsed.get("start_date"),
            "status": parsed.get("status"),
            "grade": parsed.get("grade"),
            "stage": parsed.get("stage"),
            "train": parsed.get("train"),
            "test": parsed.get("test"),
            "raw": payload,
        }

    def get_simulation_result(self, simulation_id: str) -> dict[str, Any]:
        """Resolve a simulation to its alpha metrics.

        Polls once to discover the alpha id, then fetches the alpha record.
        Raises :class:`APIError` when the simulation has not completed.
        """
        status = self.get_simulation_status(simulation_id)
        alpha_id = status.get("alpha_id")
        if not alpha_id:
            raise APIError(
                f"simulation {simulation_id} has no alpha yet "
                f"(status={status.get('status')}, progress={status.get('progress')})",
                url=status.get("url"),
                detail=str(status.get("message") or "")[:400] or None,
            )
        result = self.get_alpha(alpha_id)
        result["simulation_id"] = simulation_id
        return result

    def fetch_yearly_stats(self, alpha_id: str) -> dict[str, dict[str, float | None]]:
        """Fetch per-year statistics. Returns ``{}`` when unavailable.

        Verified endpoint: ``GET /alphas/{id}/recordsets/yearly-stats`` returns a
        JSON recordset (``schema`` + positional ``records``).

        The recordset is not necessarily ready the instant a simulation
        completes. Observed against the live API: a fetch 3 seconds after
        completion returned an empty ``text/html`` body, while the same alpha
        returned a well-formed recordset minutes later — and an alpha that had
        parsed successfully earlier was later seen returning empty ``text/html``
        too. So this retries a few times with a short delay before giving up.

        Any failure still degrades to an empty dict plus one warning, because the
        aggregate metrics remain usable and an optional enrichment call must
        never fail a batch.
        """
        url = api.yearly_stats_url(self.base_url, alpha_id, self._yearly_stats_path)
        last_reason = "no attempt made"

        for attempt in range(1, self._yearly_stats_attempts + 1):
            try:
                response = self._request("GET", url)
            except APIError as exc:
                last_reason = str(exc)
            else:
                if response.status_code != 200:
                    last_reason = f"HTTP {response.status_code}"
                else:
                    stats = api.parse_yearly_stats(self._safe_json(response))
                    if stats:
                        if attempt > 1:
                            self.log.debug(
                                "yearly stats for alpha %s became available on attempt %d",
                                alpha_id, attempt,
                            )
                        return stats
                    content_type = response.headers.get("Content-Type", "?").split(";")[0]
                    body = response.text or ""
                    last_reason = (
                        f"body was not a parseable recordset (Content-Type={content_type}, "
                        f"{len(body)} bytes)"
                    )
                    if "json" in content_type.lower():
                        # A JSON body that is not a recordset is usually an error
                        # object, and its byte count alone cannot say which. Live
                        # runs hit this, so the snippet is what makes it
                        # diagnosable after the fact.
                        last_reason += f": {body[:200]}"

            if attempt < self._yearly_stats_attempts:
                self.log.debug(
                    "yearly stats for alpha %s not ready (%s); retrying in %.0fs "
                    "(attempt %d/%d)",
                    alpha_id, last_reason, self._yearly_stats_delay,
                    attempt, self._yearly_stats_attempts,
                )
                self._sleep(self._yearly_stats_delay)

        self.log.warning(
            "yearly stats unavailable for alpha %s after %d attempt(s): %s. "
            "Yearly-stability filters will be skipped for it; re-run later or "
            "check worldquant.api.parse_yearly_stats if this persists.",
            alpha_id, self._yearly_stats_attempts, last_reason,
        )
        return {}

    def check_submission(self, alpha_id: str) -> dict[str, Any]:
        """Run ``GET /alphas/{alpha_id}/check`` and wait for the verdict.

        This is the only way to resolve ``SELF_CORRELATION``: the plain alpha
        payload reports it as ``PENDING`` indefinitely, so an alpha can look
        fully graded while never having been checked against the account's
        existing alphas.

        The endpoint is asynchronous — it answers ``200`` with an empty
        ``text/html`` body and a ``Retry-After`` header while computing — so this
        polls until JSON arrives. An unresolved check degrades to the empty shape
        (``all_passed`` False) rather than raising: "not verified" must not
        discard a simulation that already completed.
        """
        url = api.alpha_check_url(self.base_url, alpha_id)
        last_reason = "no attempt made"

        for attempt in range(1, self._yearly_stats_attempts + 1):
            retry_after: float | None = None
            try:
                response = self._request("GET", url)
            except APIError as exc:
                last_reason = str(exc)
            else:
                retry_after = _parse_retry_after(response.headers.get(api.RETRY_AFTER_HEADER))
                if response.status_code != 200:
                    last_reason = f"HTTP {response.status_code}"
                else:
                    payload = self._safe_json(response)
                    if payload is None:
                        content_type = response.headers.get("Content-Type", "?").split(";")[0]
                        last_reason = (
                            f"not JSON yet (Content-Type={content_type}, "
                            f"{len(response.text or '')} bytes)"
                        )
                    else:
                        parsed = api.parse_check_payload(payload)
                        if parsed["total"]:
                            self.log.info(
                                "Submission checks for alpha %s: %d/%d PASS%s",
                                alpha_id, parsed["passed_count"], parsed["total"],
                                "" if parsed["all_passed"]
                                else "; failing: " + "; ".join(parsed["failures"]),
                            )
                            return parsed
                        last_reason = "response contained no checks"

            if attempt < self._yearly_stats_attempts:
                delay = (
                    retry_after
                    if retry_after and retry_after > 0
                    else self._yearly_stats_delay
                )
                self.log.debug(
                    "submission check for alpha %s pending (%s); retrying in %.1fs "
                    "(attempt %d/%d)",
                    alpha_id, last_reason, delay, attempt, self._yearly_stats_attempts,
                )
                self._sleep(delay)

        self.log.warning(
            "submission checks unavailable for alpha %s after %d attempt(s): %s. "
            "Treat it as NOT verified for submission.",
            alpha_id, self._yearly_stats_attempts, last_reason,
        )
        return api.parse_check_payload(None)

    def list_self_alphas(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        """Page through ``GET /users/self/alphas`` (the account's own alphas).

        Follows BRAIN's ``next`` links until exhausted (or ``max_pages``
        pages), and returns the normalized dicts produced by
        :func:`api.parse_self_alphas_page`. Read-only; used by the registry
        importer to seed SUBMITTED history idempotently.
        """
        url = api.self_alphas_url(self.base_url)
        params: dict[str, Any] | None = {
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
        }
        results: list[dict[str, Any]] = []
        pages = 0
        while url:
            payload = self.get_json("GET", url, params=params)
            page = api.parse_self_alphas_page(payload)
            results.extend(page["results"])
            pages += 1
            if max_pages is not None and pages >= max_pages:
                break
            url = page["next"]
            params = None  # the next URL already carries its own query string
        return results

    def get_data_field(self, field_id: str) -> dict[str, Any] | None:
        """Fetch ``GET /data-fields/{field_id}``, or None when unavailable.

        Used to attribute a data field to its dataset in the experiment ledger.
        Attribution is optional enrichment, so any failure returns None instead
        of raising: it must never break a simulation.
        """
        url = f"{self.base_url}{api.DATA_FIELDS_PATH}/{field_id}"
        try:
            response = self._request("GET", url)
        except APIError as exc:
            self.log.debug("cannot resolve data field %s: %s", field_id, exc)
            return None

        if response.status_code != 200:
            self.log.debug(
                "data field %s returned HTTP %d", field_id, response.status_code
            )
            return None

        payload = self._safe_json(response)
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------ #
    # Core request plumbing
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        url: str,
        *,
        allow_relogin: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        """Send one request with timeout, rate limiting, retries and backoff.

        Retryable conditions: 429, 5xx, connection errors and read timeouts.
        ``Retry-After`` is honoured when the server sends it. A 401/403 triggers
        at most one re-login attempt, never a retry loop.
        """
        if self._closed:
            raise APIError("client is closed", url=url)

        kwargs.setdefault("timeout", self.retry.timeout)
        attempts = max(1, int(self.retry.max_retries) + 1)
        last_error: APIError | None = None

        for attempt in range(1, attempts + 1):
            self._rate_limiter.acquire()
            try:
                response = self._session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = APIError(
                    f"network error on {method} {url}: {type(exc).__name__}",
                    url=url,
                    detail=str(exc)[:300],
                )
                self.log.warning(
                    "%s %s failed (%s), attempt %d/%d",
                    method, url, type(exc).__name__, attempt, attempts,
                )
                if attempt < attempts:
                    self._backoff(attempt, url, reason=type(exc).__name__)
                    continue
                raise last_error from exc

            status = response.status_code

            if status in _UNAUTHORIZED_STATUS:
                if allow_relogin and self._try_relogin(url):
                    continue
                raise AuthError(
                    f"authentication failed for {method} {url}",
                    status_code=status,
                    url=url,
                    detail=summarize_payload(self._safe_json(response)),
                )

            if status in _RETRYABLE_STATUS:
                retry_after = _parse_retry_after(response.headers.get(api.RETRY_AFTER_HEADER))
                delay = self._compute_delay(attempt, retry_after)
                last_error = (
                    RateLimitError(
                        f"rate limited on {method} {url}",
                        status_code=status, url=url,
                        detail=summarize_payload(self._safe_json(response)),
                    )
                    if status == 429
                    else ServerError(
                        f"server error on {method} {url}",
                        status_code=status, url=url,
                        detail=summarize_payload(self._safe_json(response)),
                    )
                )
                if attempt < attempts:
                    self.log.warning(
                        "HTTP %d on %s %s, retrying in %.1fs (attempt %d/%d)",
                        status, method, url, delay, attempt, attempts,
                    )
                    self._sleep(delay)
                    continue
                self.log.error("HTTP %d on %s %s persisted after %d attempts", status, method, url, attempts)
                raise last_error

            return response

        # Unreachable in practice: the loop either returns or raises.
        raise last_error or APIError(f"request to {url} failed", url=url)

    def _compute_delay(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter, deferring to ``Retry-After``.

        ``backoff_cap`` bounds our own backoff. A server-supplied ``Retry-After``
        is honoured up to ``retry_after_cap`` instead, because retrying earlier
        than the server asked just earns another 429.
        """
        backoff = min(self.retry.backoff_cap, self.retry.backoff_base ** attempt)
        jitter = random.uniform(0, self.retry.jitter * backoff)
        delay = backoff + jitter
        if retry_after is not None and retry_after > 0:
            honoured = min(retry_after, self.retry.retry_after_cap)
            if honoured < retry_after:
                self.log.warning(
                    "server asked to wait %.0fs; capping at %.0fs", retry_after, honoured
                )
            delay = max(delay, honoured)
        return delay

    def _backoff(self, attempt: int, url: str, *, reason: str) -> None:
        delay = self._compute_delay(attempt, None)
        self.log.warning("backing off %.1fs before retrying %s (%s)", delay, url, reason)
        self._sleep(delay)

    def _try_relogin(self, url: str) -> bool:
        """Attempt exactly one re-login. Returns True when the call may be retried.

        Concurrent workers serialise here, and a worker that finds the session
        already refreshed by someone else simply retries its own request instead
        of logging in a second time.
        """
        if self.credentials is None:
            return False

        generation_at_call = self._login_generation
        with self._relogin_lock:
            if self._login_generation != generation_at_call:
                self.log.debug("session already refreshed by another worker; retrying %s", url)
                return True

            self.log.warning("HTTP 401/403 on %s; attempting one re-login", url)
            self._authenticated = False
            try:
                self.login()
            except (AuthError, CaptchaRequiredError) as exc:
                self.log.error("re-login failed: %s", exc)
                return False
            self._login_generation += 1
            return True

    def _safe_json(self, response: requests.Response) -> Any:
        """Parse a JSON body, returning ``None`` instead of raising.

        Used for error reporting, where an unparseable body must not mask the
        real HTTP failure.
        """
        try:
            return response.json()
        except ValueError:
            return None

    def get_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """``_request`` plus strict JSON decoding.

        Raises :class:`MalformedResponseError` when the body is not JSON.
        """
        response = self._request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            snippet = (response.text or "")[:200]
            raise MalformedResponseError(
                f"response from {method} {url} was not valid JSON",
                status_code=response.status_code,
                url=url,
                detail=f"{snippet!r}",
            ) from exc

    def describe_session(self) -> str:
        """Diagnostic string with all credential-bearing headers masked."""
        return (
            f"base_url={self.base_url} authenticated={self._authenticated} "
            f"headers={redact_headers(self._session.headers)}"
        )


def _parse_retry_after(value: Any) -> float | None:
    """Parse a ``Retry-After`` header. Returns ``None`` when absent/invalid."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
