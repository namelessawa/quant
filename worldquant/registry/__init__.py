"""Factor Registry / Alpha Memory System.

A separate SQLite database (``data/factor_registry.db``) that remembers every
research attempt — duplicates, failures, metric/correlation rejections, passed
and submitted alphas — keyed by expression fingerprints, and feeds an Alpha
Generation agent compact, diversity-controlled research memory.

It is deliberately layered on top of the proven backtest stack: it reuses
:class:`~worldquant.client.WorldQuantClient`,
:class:`~worldquant.storage.ResultStore` and
:class:`~worldquant.models.AlphaResult`, and never re-implements login,
simulation or correlation APIs.

Typical use::

    from worldquant.registry import FactorRegistry, pre_simulation_gate

    with FactorRegistry(config.registry.db_path, config.registry,
                        field_datasets=field_catalog) as registry:
        decision = pre_simulation_gate(registry, expression, settings)
        if not decision.passed:
            registry.mark_duplicate(factor_id, decision.reason)
            return
        # ... run the existing simulator ...
        registry.handle_completed(result, corr_records=records)
"""

from __future__ import annotations

from .clusters import CorrelationCluster, get_cluster_representatives, get_clusters
from .adapter import (
    gate_candidate,
    load_field_datasets,
    mark_submission_outcome,
    open_registry,
    record_completed,
)
from .combinations import Combination, extract_combinations
from .config import (
    ClusteringConfig,
    CorrelationConfig,
    MemoryConfig,
    NoveltyWeights,
    PreSimulationConfig,
    RegistryConfig,
    ScoringConfig,
    SimilarityConfig,
    build_registry_config,
)
from .context import build_generation_context
from .correlation import (
    CorrelationBand,
    CorrelationDecision,
    CorrelationGate,
    CorrelationStatus,
)
from .expr_parser import (
    ExpressionFeatures,
    ParseError,
    analyze_expression,
    parse_expression_ast,
)
from .failures import (
    FAILURE_LABELS,
    classify_failure,
)
from .families import FieldFamilyResolver, classify_factor_family
from .gates import (
    ACTION_REJECT_EXACT,
    ACTION_SIMULATE,
    ACTION_SKIP_LOW_NOVELTY,
    ACTION_SKIP_SIGNAL_DUPLICATE,
    DuplicateCheckResult,
    NoveltyResult,
    PreSimulationGateResult,
    calculate_novelty,
    duplicate_check,
    pre_simulation_gate,
)
from .importer import (
    fetch_field_metadata,
    import_submitted_factors,
    migrate_existing_results,
)
from .migrations import SCHEMA_VERSION
from .pipeline import (
    AcceptanceDecision,
    GateBatchResult,
    describe_corr_verdicts,
    evaluate_acceptance,
    gate_specs,
    record_results,
)
from .scoring import quality_score, research_priority
from .store import (
    CORR_TYPE_PRODUCTION,
    CORR_TYPE_SELF,
    CORR_TYPE_SELF_MAX,
    FactorRegistry,
    FactorStatus,
    RegisteredCandidate,
    SimilarFactor,
)
from .themes import classify_theme

__all__ = [
    # store / lifecycle
    "FactorRegistry",
    "FactorStatus",
    "RegisteredCandidate",
    "SimilarFactor",
    "CORR_TYPE_SELF",
    "CORR_TYPE_SELF_MAX",
    "CORR_TYPE_PRODUCTION",
    # config
    "RegistryConfig",
    "PreSimulationConfig",
    "CorrelationConfig",
    "SimilarityConfig",
    "MemoryConfig",
    "NoveltyWeights",
    "ClusteringConfig",
    "ScoringConfig",
    "build_registry_config",
    "SCHEMA_VERSION",
    # parser / families / combinations
    "ExpressionFeatures",
    "ParseError",
    "analyze_expression",
    "parse_expression_ast",
    "FieldFamilyResolver",
    "classify_factor_family",
    "Combination",
    "extract_combinations",
    # gates
    "DuplicateCheckResult",
    "NoveltyResult",
    "PreSimulationGateResult",
    "duplicate_check",
    "calculate_novelty",
    "pre_simulation_gate",
    "ACTION_SIMULATE",
    "ACTION_REJECT_EXACT",
    "ACTION_SKIP_LOW_NOVELTY",
    "ACTION_SKIP_SIGNAL_DUPLICATE",
    # correlation / scoring
    "CorrelationGate",
    "CorrelationDecision",
    "CorrelationStatus",
    "CorrelationBand",
    "quality_score",
    "research_priority",
    # taxonomy
    "classify_theme",
    "classify_failure",
    "FAILURE_LABELS",
    # clusters / context
    "CorrelationCluster",
    "get_clusters",
    "get_cluster_representatives",
    "build_generation_context",
    # importers
    "migrate_existing_results",
    "import_submitted_factors",
    "fetch_field_metadata",
    # pipeline adapter
    "open_registry",
    "gate_candidate",
    "record_completed",
    "mark_submission_outcome",
    "load_field_datasets",
    # unified research pipeline
    "gate_specs",
    "record_results",
    "evaluate_acceptance",
    "AcceptanceDecision",
    "GateBatchResult",
    "describe_corr_verdicts",
]
