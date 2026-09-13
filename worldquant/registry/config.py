"""Configuration for the Factor Registry / Alpha Memory System.

Every threshold used by the duplicate gate, the novelty engine and the
correlation gate lives here and is overridable from the ``factor_registry``
section of ``config.yaml``. Nothing is hard-coded inside the decision logic.

The registry is **opt-in**: the dataclass defaults to ``enabled=False`` so that
programs building a config without a ``factor_registry`` section keep their
historical behaviour; the shipped ``config.yaml`` explicitly enables it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..exceptions import ConfigError


@dataclass(frozen=True)
class PreSimulationConfig:
    #: Reject an exact expression+settings duplicate before spending quota.
    reject_exact_duplicate: bool = True
    #: Novelty (0..100) below which a candidate is considered uninspired.
    min_novelty: float = 15.0
    #: A template (e.g. ``rank(ts_delta(close,<WINDOW>))``) tried at least this
    #: many times is regarded as saturated.
    max_template_trials: int = 30
    #: Reject only when BOTH conditions hold: template saturated AND novelty
    #: below ``min_novelty``. When false, only exact duplicates are blocked.
    reject_if_saturated_and_low_novelty: bool = True
    #: Same canonical expression under different settings is the same *signal* and
    #: normally deserves a novelty penalty. With this many already-researched
    #: settings variants, the Level-1 signal duplicate also hard-skips (unless the
    #: run is an explicit ablation or forced).
    max_signal_experiments: int = 6


@dataclass(frozen=True)
class CorrelationConfig:
    enabled: bool = True
    #: Deprecated alias kept for backward compatibility. When a config only sets
    #: ``threshold`` it seeds both official and research cutoffs (explicit user
    #: intent wins over the new safety-buffer defaults).
    threshold: float = 0.70
    #: Official BRAIN SELF_CORRELATION hard cutoff (display/reference only).
    official_threshold: float = 0.70
    #: Local research cutoff — deliberately *inside* the official limit so the
    #: strategy stops approaching a cluster long before hitting 0.7000.
    research_threshold: float = 0.65
    #: Below this a factor is confidently diverse; between warning and research
    #: thresholds it passes but is flagged as close to the limit.
    warning_threshold: float = 0.55
    #: When true ``abs(corr)`` is used for *research* similarity. The official
    #: BRAIN ``SELF_CORRELATION`` check verdict is always recorded separately and
    #: is never rewritten by this flag.
    use_absolute_value: bool = True
    #: UNKNOWN (no/failed/pending correlation data) must never silently count as
    #: a pass for automated submission.
    allow_submit_without_corr: bool = False
    #: A PASS inside this many points of the research cutoff is explicitly marked
    #: VERY_CLOSE_TO_LIMIT.
    margin_warning: float = 0.05


@dataclass(frozen=True)
class NoveltyWeights:
    structure_weight: float = 0.30
    field_weight: float = 0.25
    family_weight: float = 0.20
    parameter_weight: float = 0.15
    combination_weight: float = 0.10

    def normalized(self) -> dict[str, float]:
        """Weights rescaled to sum to 1 (tolerates non-summing configs)."""
        raw = {
            "structure": self.structure_weight,
            "field": self.field_weight,
            "family": self.family_weight,
            "parameter": self.parameter_weight,
            "combination": self.combination_weight,
        }
        total = sum(max(0.0, value) for value in raw.values())
        if total <= 0:
            # Fall back to the documented defaults rather than dividing by zero.
            defaults = NoveltyWeights()
            return defaults.normalized()
        return {key: max(0.0, value) / total for key, value in raw.items()}


@dataclass(frozen=True)
class SimilarityConfig:
    """Weights for the deterministic two-layer similarity engine."""

    #: field_similarity = exact*w + field-family*w  (close vs vwap:
    #: exact-name Jaccard is 0 but both are PRICE, so they stay similar).
    field_exact_weight: float = 0.6
    field_family_weight: float = 0.4
    #: Structure component weights (operator SET loses AST ordering, so it is only
    #: one of several components; paths/multiset preserve the ordering).
    operator_set_weight: float = 0.20
    operator_multiset_weight: float = 0.15
    root_weight: float = 0.15
    depth_weight: float = 0.10
    path_weight: float = 0.40
    #: Overall blend.
    overall_structure_weight: float = 0.50
    overall_field_weight: float = 0.30
    overall_window_weight: float = 0.20

    def normalized(self) -> dict[str, float]:
        """Raw non-negative weights; callers normalize each subgroup themselves."""
        return {
            "field_exact": max(0.0, self.field_exact_weight),
            "field_family": max(0.0, self.field_family_weight),
            "operator_set": max(0.0, self.operator_set_weight),
            "operator_multiset": max(0.0, self.operator_multiset_weight),
            "root": max(0.0, self.root_weight),
            "depth": max(0.0, self.depth_weight),
            "path": max(0.0, self.path_weight),
            "overall_structure": max(0.0, self.overall_structure_weight),
            "overall_field": max(0.0, self.overall_field_weight),
            "overall_window": max(0.0, self.overall_window_weight),
        }


@dataclass(frozen=True)
class ClusteringConfig:
    enabled: bool = True
    #: Edge cutoff used ONLY to build the connected-component graph.
    corr_threshold: float = 0.70
    #: Saturation is decided from unbiased whole-cluster statistics rather than
    #: from the graph edges (every one of which is above ``corr_threshold`` by
    #: construction, so averaging only them inflates internal similarity).
    saturated_min_size: int = 3
    #: Fraction of all member pairs that must have an observed pairwise SELF
    #: correlation. Unobserved pairs are evidence gaps, never zero corr.
    saturated_min_coverage: float = 0.60
    #: Saturated when the mean |corr| over all OBSERVED member pairs is at/above
    #: this, OR when high_corr_density (below) clears its cutoff.
    saturated_mean_abs_corr: float = 0.65
    #: Fraction of ALL member pairs (not just observed ones) observed at/above
    #: the graph cutoff. A long 0.71-chain with weak end-to-end links stays
    #: below this and is not marked saturated.
    saturated_high_corr_density: float = 0.70


@dataclass(frozen=True)
class ScoringConfig:
    #: research_priority = quality*quality_weight + novelty*novelty_weight
    quality_weight: float = 0.6
    novelty_weight: float = 0.4


@dataclass(frozen=True)
class MemoryConfig:
    """Search-strategy thresholds that used to be module-level magic numbers.

    Only numbers that genuinely change research behaviour live here; pure math
    constants stay in code.
    """

    #: exp(-trials / scale) family/combination novelty decay.
    family_saturation_scale: float = 10.0
    combination_saturation_scale: float = 10.0
    #: A family with >=N trials and a low submission rate is saturated.
    saturated_family_min_trials: int = 20
    saturated_family_max_submit_rate: float = 0.10
    #: Families with <=N trials are suggested as underexplored.
    underexplored_family_max_trials: int = 5
    #: Family-template structures tried >=N times are "overused".
    overused_structure_min_trials: int = 5
    #: Combinations tried >=N times with almost no submissions are saturated.
    saturated_combo_min_trials: int = 10
    saturated_combo_max_submit_rate: float = 0.10
    #: Combinations with <=N trials are shown as underexplored.
    underexplored_combo_max_trials: int = 3
    #: Average abs correlation above which a combination is "high corr".
    high_corr_average: float = 0.70
    #: A corr-rejected factor with at least this quality is a "strong signal that
    #: collided with an existing cluster" — keep the idea, change the data/structure.
    high_quality_min_sharpe: float = 1.5
    high_quality_min_fitness: float = 1.5
    #: |long-short| / total above this ratio is a one-sided book.
    one_sided_book_ratio: float = 0.80
    #: test_sharpe / train_sharpe below this (with a decent train) smells like overfit.
    overfit_test_retention: float = 0.50
    overfit_min_train_sharpe: float = 1.5
    #: A same-signal settings variant (not an explicit ablation) gets its novelty
    #: multiplied by this factor.
    same_signal_novelty_factor: float = 0.35
    #: Index prefilter pool size before expensive Python-side comparison.
    neighbor_pool_limit: int = 500
    #: Deprecated: cluster saturation thresholds moved to ClusteringConfig
    #: (unbiased mean_abs_corr / coverage / density rule). Retained so existing
    #: config files keep parsing; no longer read by the clustering code.
    cluster_saturated_min_size: int = 3
    cluster_saturated_avg_corr: float = 0.70
    #: Datasets/families with <=N trials appear in the underexplored sections.
    underexplored_dataset_max_trials: int = 3


@dataclass(frozen=True)
class RegistryConfig:
    enabled: bool = False
    db_path: Path = PROJECT_ROOT / "data" / "factor_registry.db"
    pre_simulation: PreSimulationConfig = field(default_factory=PreSimulationConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    novelty: NoveltyWeights = field(default_factory=NoveltyWeights)
    similarity: SimilarityConfig = field(default_factory=SimilarityConfig)
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


_SECTION_CLASSES = {
    "pre_simulation": PreSimulationConfig,
    "correlation": CorrelationConfig,
    "novelty": NoveltyWeights,
    "similarity": SimilarityConfig,
    "clustering": ClusteringConfig,
    "scoring": ScoringConfig,
    "memory": MemoryConfig,
}


def build_registry_config(
    payload: dict[str, Any] | None,
    *,
    data_dir: Path | None = None,
) -> RegistryConfig:
    """Parse the ``factor_registry`` config section.

    Unknown keys raise (mirroring the rest of the project's config loader), and
    a relative ``db_path`` is resolved against ``data_dir`` / the project root.
    """
    if not payload:
        return RegistryConfig()
    if not isinstance(payload, dict):
        raise ConfigError("config section 'factor_registry' must be a mapping")

    valid_top = {"enabled", "db_path", *_SECTION_CLASSES.keys()}
    unknown_top = set(payload) - valid_top
    if unknown_top:
        raise ConfigError(
            f"unknown keys in 'factor_registry': {sorted(unknown_top)}. "
            f"Valid keys: {sorted(valid_top)}"
        )

    kwargs: dict[str, Any] = {}
    if "enabled" in payload:
        kwargs["enabled"] = bool(payload["enabled"])
    if "db_path" in payload and payload["db_path"] is not None:
        db_path = Path(str(payload["db_path"]))
        if not db_path.is_absolute():
            base = data_dir or PROJECT_ROOT / "data"
            db_path = base / db_path
        kwargs["db_path"] = db_path

    for key, cls in _SECTION_CLASSES.items():
        section = payload.get(key)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"factor_registry.{key} must be a mapping")
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(section) - valid
        if unknown:
            raise ConfigError(
                f"unknown keys in factor_registry.{key}: {sorted(unknown)}. "
                f"Valid keys: {sorted(valid)}"
            )
        kwargs[key] = cls(**section)

    # Backward compatibility: an old-style config that only pins ``threshold``
    # expects that exact cutoff to be honoured, so seed the new layered thresholds
    # from it unless they were set explicitly.
    corr_section = payload.get("correlation") or {}
    if "threshold" in corr_section:
        corr_kwargs = kwargs["correlation"].__dict__.copy() if "correlation" in kwargs else {}
        explicit = float(corr_section["threshold"])
        corr_kwargs.setdefault("official_threshold", explicit)
        corr_kwargs.setdefault("research_threshold", explicit)
        corr_kwargs["threshold"] = explicit
        kwargs["correlation"] = CorrelationConfig(**corr_kwargs)

    config = replace(RegistryConfig(**kwargs)) if kwargs else RegistryConfig()

    def _within_01(name: str, value: float) -> None:
        if not 0.0 <= value <= 1.0:
            raise ConfigError(
                f"factor_registry.correlation.{name} must be within [0, 1], got {value}"
            )

    _within_01("official_threshold", config.correlation.official_threshold)
    _within_01("research_threshold", config.correlation.research_threshold)
    _within_01("warning_threshold", config.correlation.warning_threshold)
    _within_01("threshold", config.correlation.threshold)
    if not (
        config.correlation.warning_threshold
        <= config.correlation.research_threshold
        <= config.correlation.official_threshold
    ):
        raise ConfigError(
            "factor_registry.correlation thresholds must satisfy "
            "warning_threshold <= research_threshold <= official_threshold"
        )
    if not 0.0 <= config.clustering.corr_threshold <= 1.0:
        raise ConfigError(
            "factor_registry.clustering.corr_threshold must be within [0, 1], got "
            f"{config.clustering.corr_threshold}"
        )
    if config.clustering.saturated_min_size < 2:
        raise ConfigError(
            "factor_registry.clustering.saturated_min_size must be >= 2"
        )
    for name in (
        "saturated_min_coverage",
        "saturated_mean_abs_corr",
        "saturated_high_corr_density",
    ):
        _within_01(name, getattr(config.clustering, name))
    if config.pre_simulation.min_novelty < 0:
        raise ConfigError("factor_registry.pre_simulation.min_novelty cannot be negative")
    if config.pre_simulation.max_template_trials < 1:
        raise ConfigError("factor_registry.pre_simulation.max_template_trials must be >= 1")
    if config.pre_simulation.max_signal_experiments < 1:
        raise ConfigError(
            "factor_registry.pre_simulation.max_signal_experiments must be >= 1"
        )
    return config
