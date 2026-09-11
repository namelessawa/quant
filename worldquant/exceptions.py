"""Exception hierarchy for the WorldQuant BRAIN tooling.

Every exception carries enough context (url, status code, alpha expression) that
a failure can be diagnosed from the log line alone, without re-running the batch.
"""

from __future__ import annotations

from typing import Any


class WorldQuantError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(WorldQuantError):
    """Invalid or missing configuration / credentials."""


class StorageError(WorldQuantError):
    """Local SQLite or CSV persistence failed."""


class APIError(WorldQuantError):
    """The BRAIN backend returned a response we cannot turn into a result.

    Attributes:
        status_code: HTTP status code, ``None`` when no response was received.
        url: Request URL.
        detail: Short, redacted description of the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        detail: str | None = None,
    ) -> None:
        parts = [message]
        if status_code is not None:
            parts.append(f"status={status_code}")
        if url:
            parts.append(f"url={url}")
        if detail:
            parts.append(f"detail={detail}")
        super().__init__(" | ".join(parts))
        self.status_code = status_code
        self.url = url
        self.detail = detail


class AuthError(APIError):
    """Credentials were rejected or the session expired.

    Callers must not retry blindly: re-login once, then surface the error.
    """


class CaptchaRequiredError(AuthError):
    """The platform asked for an interactive challenge (biometric / persona).

    This tool deliberately does NOT attempt to solve or bypass it. The run stops
    so a human can complete the challenge in a browser.
    """


class RateLimitError(APIError):
    """HTTP 429 — retried with backoff by the client, raised when exhausted."""


class ServerError(APIError):
    """HTTP 5xx — retried with backoff by the client, raised when exhausted."""


class MalformedResponseError(APIError):
    """The response was not JSON, or lacked the fields we need."""


class SimulationError(WorldQuantError):
    """Base class for simulation lifecycle failures."""

    def __init__(self, message: str, *, expression: str | None = None) -> None:
        super().__init__(message if not expression else f"{message} | expr={expression!r}")
        self.expression = expression


class SimulationFailedError(SimulationError):
    """BRAIN reported the simulation as FAIL / ERROR."""

    def __init__(
        self,
        message: str,
        *,
        expression: str | None = None,
        simulation_id: str | None = None,
    ) -> None:
        super().__init__(message, expression=expression)
        self.simulation_id = simulation_id


class SimulationTimeoutError(SimulationError):
    """Polling exceeded ``max_wait``. State is persisted so the run can resume."""

    def __init__(
        self,
        message: str,
        *,
        expression: str | None = None,
        simulation_id: str | None = None,
    ) -> None:
        super().__init__(message, expression=expression)
        self.simulation_id = simulation_id


def summarize_payload(payload: Any, limit: int = 400) -> str:
    """Return a short, single-line, credential-free summary of a JSON payload.

    Response bodies are truncated and sensitive keys are dropped before they
    reach the log or an exception message.
    """
    sensitive = {"password", "token", "authorization", "cookie", "secret", "inquiry"}

    def scrub(obj: Any, depth: int = 0) -> Any:
        if depth > 3:
            return "..."
        if isinstance(obj, dict):
            return {
                k: ("<redacted>" if str(k).lower() in sensitive else scrub(v, depth + 1))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [scrub(v, depth + 1) for v in obj[:5]]
        return obj

    text = repr(scrub(payload))
    if len(text) > limit:
        text = text[:limit] + "...<truncated>"
    return text
