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


@dataclass(frozen=True)
class CorrelationConfig:
    enabled: bool = True
    #: Research/submission cutoff. Mirrors the project's hard 0.70 rule.
    threshold: float = 0.70
    #: When true ``abs(corr)`` is used for *research* similarity. The official
    #: BRAIN ``SELF_CORRELATION`` check verdict is always recorded separately and
    #: is never rewritten by this flag.
    use_absolute_value: bool = True


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
class ClusteringConfig:
    enabled: bool = True
    corr_threshold: float = 0.70


@dataclass(frozen=True)
class ScoringConfig:
    #: research_priority = quality*quality_weight + novelty*novelty_weight
    quality_weight: float = 0.6
    novelty_weight: float = 0.4


@dataclass(frozen=True)
class RegistryConfig:
    enabled: bool = False
    db_path: Path = PROJECT_ROOT / "data" / "factor_registry.db"
    pre_simulation: PreSimulationConfig = field(default_factory=PreSimulationConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    novelty: NoveltyWeights = field(default_factory=NoveltyWeights)
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)


_SECTION_CLASSES = {
    "pre_simulation": PreSimulationConfig,
    "correlation": CorrelationConfig,
    "novelty": NoveltyWeights,
    "clustering": ClusteringConfig,
    "scoring": ScoringConfig,
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

    config = replace(RegistryConfig(**kwargs)) if kwargs else RegistryConfig()

    if not 0.0 <= config.correlation.threshold <= 1.0:
        raise ConfigError(
            f"factor_registry.correlation.threshold must be within [0, 1], got "
            f"{config.correlation.threshold}"
        )
    if not 0.0 <= config.clustering.corr_threshold <= 1.0:
        raise ConfigError(
            "factor_registry.clustering.corr_threshold must be within [0, 1], got "
            f"{config.clustering.corr_threshold}"
        )
    if config.pre_simulation.min_novelty < 0:
        raise ConfigError("factor_registry.pre_simulation.min_novelty cannot be negative")
    if config.pre_simulation.max_template_trials < 1:
        raise ConfigError("factor_registry.pre_simulation.max_template_trials must be >= 1")
    return config
