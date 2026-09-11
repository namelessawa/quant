"""WorldQuant BRAIN alpha backtesting and screening toolkit.

Typical use::

    from worldquant import AppConfig, WorldQuantClient, ResultStore, SimulationRunner, load_config

    config = load_config("config.yaml")
    with WorldQuantClient(config.credentials, base_url=config.base_url) as client:
        with ResultStore(config.storage.db_path) as store:
            runner = SimulationRunner(client, store, config)
            results = runner.run_batch(specs)

See ``scripts/run_backtest.py`` for the full command-line workflow.
"""

from __future__ import annotations

from .api import DEFAULT_SETTINGS, AlphaGrade, SimulationStatus
from .client import WorldQuantClient
from .config import (
    DEFAULT_CREDENTIALS_FILENAME,
    AppConfig,
    Credentials,
    FilterConfig,
    RetryConfig,
    RunnerConfig,
    StorageConfig,
    credentials_from_env,
    credentials_from_file,
    load_config,
    require_credentials,
    resolve_credentials,
    validate_filter_config,
)
from .exceptions import (
    APIError,
    AuthError,
    CaptchaRequiredError,
    ConfigError,
    MalformedResponseError,
    RateLimitError,
    ServerError,
    SimulationError,
    SimulationFailedError,
    SimulationTimeoutError,
    StorageError,
    WorldQuantError,
)
from .experiment_log import (
    LEDGER_COLUMNS,
    ExperimentLog,
    FieldCatalog,
    build_ledger,
    extract_field_names,
    failed_checks,
    write_ledger_from_results,
)
from .filters import ThresholdFilter, YearlyStabilityFilter, evaluate_filters
from .generator import AlphaGenerator, CombinatorialGenerator, StaticGenerator
from .hashing import (
    SCOPE_SETTINGS,
    auto_alpha_id,
    dedup_key,
    expression_hash,
    normalize_expression,
    scope_hash,
)
from .loader import load_alphas, specs_from_expressions
from .logging_utils import get_logger, setup_logging
from .models import AlphaResult, AlphaSpec, FilterOutcome, YearlySummary, summarize_yearly
from .ranking import format_leaderboard, latest_per_alpha, rank_results
from .registry import (
    CorrelationGate,
    FactorRegistry,
    FactorStatus,
    build_generation_context,
    gate_candidate,
    import_submitted_factors,
    migrate_existing_results,
    open_registry,
    pre_simulation_gate,
    record_completed,
)
from .simulator import SimulationRunner
from .storage import ResultStore

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # api / config
    "DEFAULT_SETTINGS",
    "SimulationStatus",
    "AlphaGrade",
    "AppConfig",
    "Credentials",
    "FilterConfig",
    "RetryConfig",
    "RunnerConfig",
    "StorageConfig",
    "load_config",
    "require_credentials",
    "validate_filter_config",
    "credentials_from_env",
    "credentials_from_file",
    "resolve_credentials",
    "DEFAULT_CREDENTIALS_FILENAME",
    # client / storage / runner
    "WorldQuantClient",
    "ResultStore",
    "SimulationRunner",
    # models
    "AlphaResult",
    "AlphaSpec",
    "FilterOutcome",
    "YearlySummary",
    "summarize_yearly",
    # hashing / loading / generation
    "auto_alpha_id",
    "dedup_key",
    "expression_hash",
    "scope_hash",
    "SCOPE_SETTINGS",
    "normalize_expression",
    "load_alphas",
    "specs_from_expressions",
    "AlphaGenerator",
    "CombinatorialGenerator",
    "StaticGenerator",
    # filters / ranking
    "ThresholdFilter",
    "YearlyStabilityFilter",
    "evaluate_filters",
    "format_leaderboard",
    "latest_per_alpha",
    "rank_results",
    # factor registry / alpha memory
    "FactorRegistry",
    "FactorStatus",
    "CorrelationGate",
    "pre_simulation_gate",
    "build_generation_context",
    "migrate_existing_results",
    "import_submitted_factors",
    "open_registry",
    "gate_candidate",
    "record_completed",
    # experiment ledger
    "LEDGER_COLUMNS",
    "ExperimentLog",
    "FieldCatalog",
    "build_ledger",
    "extract_field_names",
    "failed_checks",
    "write_ledger_from_results",
    # logging
    "get_logger",
    "setup_logging",
    # exceptions
    "WorldQuantError",
    "ConfigError",
    "StorageError",
    "APIError",
    "AuthError",
    "CaptchaRequiredError",
    "RateLimitError",
    "ServerError",
    "MalformedResponseError",
    "SimulationError",
    "SimulationFailedError",
    "SimulationTimeoutError",
]
