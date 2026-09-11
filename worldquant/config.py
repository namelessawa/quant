"""Configuration loading.

Credentials come only from the environment (``WQBRAIN_USERNAME`` /
``WQBRAIN_PASSWORD``), optionally seeded from a ``.env`` file. Everything else —
backtest settings, filter thresholds, polling cadence, storage paths — can be
overridden from a YAML or JSON config file and then again from the CLI.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .api import DEFAULT_BASE_URL, DEFAULT_SETTINGS, DEFAULT_YEARLY_STATS_PATH
from .exceptions import ConfigError

if TYPE_CHECKING:  # runtime import is delayed (registry.config imports this module)
    from .registry.config import RegistryConfig

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Hard ceiling on parallel simulations. BRAIN rate-limits aggressively and the
#: task requires conservative concurrency, so this is not configurable.
MAX_CONCURRENCY = 3

ENV_USERNAME = "WQBRAIN_USERNAME"
ENV_PASSWORD = "WQBRAIN_PASSWORD"
ENV_CREDENTIALS_FILE = "WQBRAIN_CREDENTIALS_FILE"
ENV_BASE_URL = "WQBRAIN_BASE_URL"
ENV_DB_PATH = "WQBRAIN_DB_PATH"
ENV_LOG_LEVEL = "WQBRAIN_LOG_LEVEL"
ENV_YEARLY_PATH = "WQBRAIN_YEARLY_PATH"

#: Auto-discovered plaintext credential file, relative to the project root.
DEFAULT_CREDENTIALS_FILENAME = "credentials.json"

#: Values meaning "this is still the shipped template". Such a file is treated as
#: absent rather than handed to the server as a literal password.
PLACEHOLDER_VALUES = frozenset(
    {
        "", "changeme", "change-me", "placeholder", "todo", "xxx",
        "your-password", "your_password", "your-email", "your_username",
        "you@example.com", "email@example.com", "example.com",
        "<email>", "<password>", "<username>", "none", "null",
    }
)


@dataclass(frozen=True)
class Credentials:
    """BRAIN login. Never log or persist the password."""

    username: str
    password: str

    def __repr__(self) -> str:  # keep the password out of logs and tracebacks
        return f"Credentials(username={self.username!r}, password=<redacted>)"


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 5
    backoff_base: float = 2.0
    #: Bounds our own exponential backoff.
    backoff_cap: float = 60.0
    #: Bounds how long we will honour a server-supplied ``Retry-After``. A
    #: generous ceiling, because ignoring the server's explicit request just
    #: earns more 429s — but an absurd value must not stall the run forever.
    retry_after_cap: float = 120.0
    timeout: float = 30.0
    jitter: float = 0.3


@dataclass(frozen=True)
class RunnerConfig:
    poll_interval: float = 10.0
    poll_jitter: float = 3.0
    max_wait: float = 1800.0
    concurrency: int = 2
    #: Minimum spacing between any two HTTP requests, across all workers.
    min_request_interval: float = 1.0


@dataclass(frozen=True)
class FilterConfig:
    """Thresholds for the automatic pass/fail decision.

    ``None`` disables a rule. Turnover / returns / drawdown are decimal
    fractions, matching what BRAIN returns (0.70 == 70%).
    """

    min_sharpe: float | None = 1.25
    min_fitness: float | None = 1.0
    max_turnover: float | None = 0.70
    min_returns: float | None = None
    max_drawdown: float | None = None
    min_margin: float | None = None

    # Yearly stability rules. Skipped entirely when no yearly data is available,
    # so a missing yearly-stats endpoint never fails an otherwise good alpha.
    min_positive_year_ratio: float | None = 0.6
    max_negative_sharpe_years: int | None = 1
    min_worst_year_sharpe: float | None = None
    max_yearly_sharpe_std: float | None = None
    #: When true, an alpha with no yearly data fails instead of being excused.
    require_yearly_data: bool = False


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path = PROJECT_ROOT / "data" / "worldquant.db"
    data_dir: Path = PROJECT_ROOT / "data"
    log_file: Path = PROJECT_ROOT / "logs" / "worldquant.log"
    log_level: str = "INFO"
    #: Append-only xlsx ledger, one row per simulation.
    ledger_path: Path = PROJECT_ROOT / "data" / "experiments.xlsx"
    #: Local cache mapping a data field id to its dataset id.
    field_catalog_path: Path = PROJECT_ROOT / "data" / "field_catalog.json"


def _default_registry_config() -> "RegistryConfig":
    """Delayed construction to dodge the registry.config <-> config cycle."""
    from .registry.config import RegistryConfig

    return RegistryConfig()


@dataclass(frozen=True)
class AppConfig:
    credentials: Credentials | None = None
    base_url: str = DEFAULT_BASE_URL
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    filters: FilterConfig = field(default_factory=FilterConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    fetch_yearly_stats: bool = True
    yearly_stats_path: str = DEFAULT_YEARLY_STATS_PATH
    #: Factor Registry / Alpha Memory System. Disabled by default; opt-in via
    #: the ``factor_registry`` section of config.yaml.
    registry: "RegistryConfig" = field(default_factory=_default_registry_config)


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
def load_dotenv_if_available(root: Path | None = None) -> None:
    """Load ``.env`` when python-dotenv is installed. Real env vars win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv not installed; relying on process environment")
        return
    env_file = (root or PROJECT_ROOT) / ".env"
    load_dotenv(env_file, override=False)


def _read_config_file(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = PROJECT_ROOT / file_path
    if not file_path.exists():
        raise ConfigError(f"config file not found: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("PyYAML is required to read .yaml config files") from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a mapping, got {type(data).__name__}")
    return data


def _build_dataclass(cls: type, payload: dict[str, Any] | None, section: str) -> Any:
    """Instantiate a frozen config dataclass, rejecting unknown keys loudly."""
    if not payload:
        return cls()
    if not isinstance(payload, dict):
        raise ConfigError(f"config section {section!r} must be a mapping")

    valid = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(payload) - valid
    if unknown:
        raise ConfigError(
            f"unknown keys in config section {section!r}: {sorted(unknown)}. "
            f"Valid keys: {sorted(valid)}"
        )
    return cls(**payload)


def credentials_from_env() -> Credentials | None:
    username = os.environ.get(ENV_USERNAME, "").strip()
    password = os.environ.get(ENV_PASSWORD, "")
    if not username and not password:
        return None
    if not username or not password:
        raise ConfigError(
            f"{ENV_USERNAME} and {ENV_PASSWORD} must both be set; "
            "found only one of them"
        )
    return Credentials(username=username, password=password)


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_VALUES


def _warn_if_loosely_protected(path: Path, *, posix: bool | None = None) -> None:
    """The credentials file holds a plaintext password; warn when it is exposed.

    ``posix`` is an explicit override for tests. Detecting the platform by
    monkeypatching ``os.name`` is not viable: ``pathlib`` chooses ``PosixPath``
    vs ``WindowsPath`` from it, so flipping it on Windows raises
    ``NotImplementedError`` deep inside the test runner.
    """
    if not ((os.name == "posix") if posix is None else posix):
        # st_mode cannot express NTFS ACLs, so there is nothing meaningful to check.
        logger.warning(
            "%s stores your BRAIN password in plaintext; make sure your OS user "
            "account is the only one with read access to it",
            path,
        )
        return

    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        logger.warning(
            "%s is readable by group/others (mode %o). Tighten it with: chmod 600 %s",
            path, mode, path,
        )


def credentials_from_file(path: str | Path, *, strict: bool = True) -> Credentials | None:
    """Load credentials from a JSON file shaped like ``{"email": ..., "password": ...}``.

    ``username`` is accepted as an alias for ``email``. The password is used
    verbatim (no stripping) and is never logged.

    Args:
        strict: When True the file was explicitly requested, so anything wrong
            raises. When False it was auto-discovered, and a missing file or an
            unfilled template returns ``None`` so the next source can be tried.
            Malformed or incomplete content raises either way, because silently
            ignoring a file the user did create is more confusing than helpful.
    """
    file_path = Path(path)

    if not file_path.exists():
        if strict:
            raise ConfigError(f"credentials file not found: {file_path}")
        return None

    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read credentials file {file_path}: {exc}") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Report the position, never the content: it contains the password.
        raise ConfigError(
            f"credentials file {file_path} is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(payload, dict):
        raise ConfigError(
            f"credentials file {file_path} must contain a JSON object, "
            f"got {type(payload).__name__}"
        )

    lowered = {str(key).strip().lower(): value for key, value in payload.items()}
    username = lowered.get("email", lowered.get("username"))
    password = lowered.get("password")

    if not isinstance(username, str) or not isinstance(password, str):
        missing = [
            name
            for name, value in (("email (or username)", username), ("password", password))
            if not isinstance(value, str)
        ]
        raise ConfigError(
            f"credentials file {file_path} is missing or has non-string values for: "
            f"{', '.join(missing)}. Expected "
            '{"email": "you@example.com", "password": "..."}'
        )

    if _is_placeholder(username) or _is_placeholder(password):
        if strict:
            raise ConfigError(
                f"credentials file {file_path} still contains placeholder values; "
                "put your real BRAIN email and password in it"
            )
        logger.debug("%s still holds placeholder values; ignoring it", file_path)
        return None

    username = username.strip()
    if not username:
        raise ConfigError(f"credentials file {file_path} has an empty email/username")

    _warn_if_loosely_protected(file_path)
    logger.info("Loaded credentials for %s from %s", username, file_path)
    return Credentials(username=username, password=password)


def resolve_credentials(
    root: Path | None = None,
    explicit_path: str | Path | None = None,
) -> Credentials | None:
    """Find credentials, highest precedence first.

    1. ``explicit_path`` — the ``--credentials`` CLI flag
    2. ``WQBRAIN_CREDENTIALS_FILE`` — an explicitly named JSON file
    3. ``WQBRAIN_USERNAME`` / ``WQBRAIN_PASSWORD`` (including values from ``.env``)
    4. ``<project root>/credentials.json`` — auto-discovered

    An explicitly named file must be usable, so problems there raise. The
    auto-discovered file is a convenience, so an absent or unfilled one simply
    falls through.
    """
    base_root = Path(root) if root else PROJECT_ROOT

    if explicit_path:
        return credentials_from_file(explicit_path, strict=True)

    from_env_path = os.environ.get(ENV_CREDENTIALS_FILE, "").strip()
    if from_env_path:
        path = Path(from_env_path)
        if not path.is_absolute():
            path = base_root / path
        return credentials_from_file(path, strict=True)

    from_env = credentials_from_env()
    if from_env is not None:
        return from_env

    return credentials_from_file(base_root / DEFAULT_CREDENTIALS_FILENAME, strict=False)


def _clamp_concurrency(value: int) -> int:
    if value < 1:
        logger.warning("concurrency %s < 1; using 1", value)
        return 1
    if value > MAX_CONCURRENCY:
        logger.warning(
            "concurrency %s exceeds the conservative ceiling of %d; clamping to %d "
            "to avoid hammering the BRAIN backend",
            value, MAX_CONCURRENCY, MAX_CONCURRENCY,
        )
        return MAX_CONCURRENCY
    return value


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    root: Path | None = None,
    credentials_path: str | Path | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig`.

    Precedence, lowest to highest: built-in defaults < config file < environment
    variables < explicit ``overrides`` (used by the CLI).

    Credentials are resolved separately by :func:`resolve_credentials`;
    ``credentials_path`` is the highest-precedence source.
    """
    load_dotenv_if_available(root)
    base_root = root or PROJECT_ROOT

    raw: dict[str, Any] = _read_config_file(path) if path else {}

    settings = dict(DEFAULT_SETTINGS)
    file_settings = raw.get("settings")
    if file_settings is not None:
        if not isinstance(file_settings, dict):
            raise ConfigError("config section 'settings' must be a mapping")
        settings.update({k: v for k, v in file_settings.items() if v is not None})

    storage_payload = dict(raw.get("storage") or {})
    storage_payload.setdefault("data_dir", str(base_root / "data"))
    if "db_path" not in storage_payload:
        storage_payload["db_path"] = str(Path(storage_payload["data_dir"]) / "worldquant.db")
    if "log_file" not in storage_payload:
        storage_payload["log_file"] = str(base_root / "logs" / "worldquant.log")

    storage_payload.setdefault("ledger_path", str(Path(storage_payload["data_dir"]) / "experiments.xlsx"))
    storage_payload.setdefault(
        "field_catalog_path", str(Path(storage_payload["data_dir"]) / "field_catalog.json")
    )

    storage = StorageConfig(
        db_path=Path(os.environ.get(ENV_DB_PATH) or storage_payload["db_path"]),
        data_dir=Path(storage_payload["data_dir"]),
        log_file=Path(storage_payload["log_file"]),
        log_level=str(
            os.environ.get(ENV_LOG_LEVEL) or storage_payload.get("log_level", "INFO")
        ).upper(),
        ledger_path=Path(storage_payload["ledger_path"]),
        field_catalog_path=Path(storage_payload["field_catalog_path"]),
    )

    runner_payload = dict(raw.get("runner") or {})
    runner = _build_dataclass(RunnerConfig, runner_payload, "runner")
    runner = replace(runner, concurrency=_clamp_concurrency(runner.concurrency))

    filters = _build_dataclass(FilterConfig, raw.get("filters"), "filters")
    retry = _build_dataclass(RetryConfig, raw.get("retry"), "retry")

    yearly_payload = raw.get("yearly_stats") or {}
    if not isinstance(yearly_payload, dict):
        raise ConfigError("config section 'yearly_stats' must be a mapping")

    # Delayed import: registry.config imports PROJECT_ROOT from this module.
    from .registry.config import build_registry_config

    registry = build_registry_config(
        raw.get("factor_registry"),
        data_dir=Path(storage_payload["data_dir"]),
    )

    config = AppConfig(
        credentials=resolve_credentials(base_root, credentials_path),
        base_url=str(
            os.environ.get(ENV_BASE_URL) or raw.get("base_url") or DEFAULT_BASE_URL
        ).rstrip("/"),
        settings=settings,
        filters=filters,
        runner=runner,
        retry=retry,
        storage=storage,
        fetch_yearly_stats=bool(yearly_payload.get("enabled", True)),
        yearly_stats_path=str(
            os.environ.get(ENV_YEARLY_PATH)
            or yearly_payload.get("path")
            or DEFAULT_YEARLY_STATS_PATH
        ),
        registry=registry,
    )

    if overrides:
        config = apply_overrides(config, overrides)
    return config


_RUNNER_KEYS = frozenset(
    {"poll_interval", "poll_jitter", "max_wait", "concurrency", "min_request_interval"}
)
_TOP_LEVEL_KEYS = frozenset(
    {"base_url", "fetch_yearly_stats", "yearly_stats_path", "credentials"}
)


def apply_overrides(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """Apply CLI-level overrides. Unknown keys raise instead of being ignored."""
    allowed = _RUNNER_KEYS | _TOP_LEVEL_KEYS | {"settings"}
    unknown = set(overrides) - allowed
    if unknown:
        raise ConfigError(
            f"unknown configuration overrides: {sorted(unknown)}. "
            f"Valid keys: {sorted(allowed)}"
        )

    runner_changes = {
        key: overrides[key] for key in _RUNNER_KEYS if overrides.get(key) is not None
    }
    if "concurrency" in runner_changes:
        runner_changes["concurrency"] = _clamp_concurrency(int(runner_changes["concurrency"]))
    runner = replace(config.runner, **runner_changes) if runner_changes else config.runner

    settings = dict(config.settings)
    if overrides.get("settings"):
        settings.update(overrides["settings"])

    top_level = {
        key: overrides[key] for key in _TOP_LEVEL_KEYS if overrides.get(key) is not None
    }
    return replace(config, runner=runner, settings=settings, **top_level)


def require_credentials(config: AppConfig) -> Credentials:
    """Return credentials or raise a helpful error. Never logs the password."""
    if config.credentials is None:
        raise ConfigError(
            "missing credentials. Provide them in any one of these ways: "
            f"(1) fill in {DEFAULT_CREDENTIALS_FILENAME} in the project root "
            '(copy credentials.example.json), shaped {"email": "...", "password": "..."}; '
            f"(2) point --credentials PATH or {ENV_CREDENTIALS_FILE} at such a file; "
            f"(3) set {ENV_USERNAME} and {ENV_PASSWORD} in the environment or a .env file"
        )
    return config.credentials


def validate_filter_config(filters: FilterConfig) -> None:
    """Fail fast on nonsense thresholds rather than silently passing everything."""
    ratio = filters.min_positive_year_ratio
    if ratio is not None and not 0.0 <= ratio <= 1.0:
        raise ConfigError(
            f"filters.min_positive_year_ratio must be within [0, 1], got {ratio}"
        )
    for name in ("min_sharpe", "min_fitness", "min_returns", "min_margin",
                 "max_drawdown", "min_worst_year_sharpe", "max_yearly_sharpe_std"):
        value = getattr(filters, name)
        if value is not None and not isinstance(value, (int, float)):
            raise ConfigError(f"filters.{name} must be a number or null, got {value!r}")
    if filters.max_turnover is not None and filters.max_turnover > 1.0:
        logger.warning(
            "filters.max_turnover=%s looks like a percent value; turnover is stored "
            "as a decimal fraction, so 70%% should be written as 0.70",
            filters.max_turnover,
        )
    if filters.max_negative_sharpe_years is not None and filters.max_negative_sharpe_years < 0:
        raise ConfigError("filters.max_negative_sharpe_years cannot be negative")
