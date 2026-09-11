"""Logging setup shared by the CLI and the library.

Logs go to both the console and ``logs/worldquant.log``. Headers are passed
through :func:`redact_headers` before they are ever formatted, so the Basic-Auth
credential header cannot leak into the log file.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Mapping

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})

ROOT_LOGGER_NAME = "worldquant"


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Return a copy of ``headers`` with credential-bearing values masked."""
    if not headers:
        return {}
    return {
        str(key): ("<redacted>" if str(key).lower() in _SENSITIVE_HEADERS else str(value))
        for key, value in headers.items()
    }


def setup_logging(
    log_file: str | Path | None = None,
    level: str | int = "INFO",
    *,
    logger_name: str = ROOT_LOGGER_NAME,
) -> logging.Logger:
    """Attach console (and optional file) handlers to the package logger.

    Safe to call repeatedly: existing handlers added by this function are
    replaced rather than duplicated, so a long batch run does not emit every
    line twice.
    """
    logger = logging.getLogger(logger_name)
    resolved_level = logging.getLevelName(str(level).upper()) if isinstance(level, str) else level
    if not isinstance(resolved_level, int):
        resolved_level = logging.INFO
    logger.setLevel(resolved_level)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_wq_managed", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(resolved_level)
    console.setFormatter(formatter)
    console._wq_managed = True  # type: ignore[attr-defined]
    logger.addHandler(console)

    if log_file is not None:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot write log file %s (%s); continuing with console only", path, exc)
        else:
            file_handler.setLevel(resolved_level)
            file_handler.setFormatter(formatter)
            file_handler._wq_managed = True  # type: ignore[attr-defined]
            logger.addHandler(file_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the package logger, e.g. ``get_logger("client")``."""
    if not name:
        return logging.getLogger(ROOT_LOGGER_NAME)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
