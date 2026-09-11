"""Deterministic, explainable factor scoring.

Two independent scores are kept apart, as required:

* :func:`quality_score` — how good the alpha is (Sharpe / Fitness / turnover /
  drawdown), on a 0..100 scale.
* :class:`~worldquant.registry.gates.NoveltyResult` — how original the research
  direction is, computed elsewhere on structural features.

:func:`research_priority` blends the two with configurable weights
(``ScoringConfig``, default 0.6 quality / 0.4 novelty). Novelty never masquerades
as quality and vice versa.
"""

from __future__ import annotations

from .config import ScoringConfig


def _unit_clip(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _sharpe_component(sharpe: float | None) -> float:
    """Sharpe 2.5+ saturates to 1.0; the BRAIN submission floor is 1.25."""
    component = _unit_clip((sharpe / 2.5) if sharpe is not None else None)
    return 0.0 if component is None else component


def _fitness_component(fitness: float | None) -> float:
    """Fitness 2.5+ saturates to 1.0; the submission floor is 1.0."""
    component = _unit_clip((fitness / 2.5) if fitness is not None else None)
    return 0.0 if component is None else component


def _turnover_component(turnover: float | None) -> float:
    """1.0 inside the healthy 1%..50% band, linearly decaying to its edges."""
    if turnover is None:
        return 0.5  # unknown -> neutral, neither rewarded nor punished hard
    turnover = float(turnover)
    if turnover < 0.0:
        return 0.0
    if 0.01 <= turnover <= 0.50:
        return 1.0
    if turnover < 0.01:
        # 0 turnover -> 0; reaches 1 at the 0.01 floor.
        return max(0.0, min(1.0, turnover / 0.01))
    # 0.50 -> 1.0, 1.00+ -> 0.0
    return max(0.0, min(1.0, 1.0 - (turnover - 0.50) / 0.50))


def _drawdown_component(drawdown: float | None) -> float:
    """Drawdown 0% -> 1.0; 50% or worse -> 0.0. BRAIN reports it negative."""
    if drawdown is None:
        return 0.5
    magnitude = abs(float(drawdown))
    return max(0.0, min(1.0, 1.0 - magnitude / 0.50))


def quality_score(
    sharpe: float | None,
    fitness: float | None,
    turnover: float | None = None,
    drawdown: float | None = None,
) -> float | None:
    """Explainable 0..100 quality score.

    ``None`` only when *both* Sharpe and Fitness are missing: a row with no
    performance signal cannot be scored. Partial metrics degrade their own
    components to neutral/zero instead of discarding the alpha.
    """
    if sharpe is None and fitness is None:
        return None
    score = (
        0.45 * _sharpe_component(sharpe)
        + 0.45 * _fitness_component(fitness)
        + 0.05 * _turnover_component(turnover)
        + 0.05 * _drawdown_component(drawdown)
    ) * 100.0
    return round(score, 2)


def research_priority(
    quality: float | None,
    novelty: float | None,
    config: ScoringConfig | None = None,
) -> float | None:
    """``quality*w + novelty*w`` on the shared 0..100 scale; ``None`` if unknown."""
    if quality is None and novelty is None:
        return None
    config = config or ScoringConfig()
    total_weight = config.quality_weight + config.novelty_weight
    if total_weight <= 0:
        return None
    quality_value = 0.0 if quality is None else float(quality)
    novelty_value = 0.0 if novelty is None else float(novelty)
    score = (
        config.quality_weight * quality_value
        + config.novelty_weight * novelty_value
    ) / total_weight
    return round(score, 2)
