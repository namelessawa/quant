"""Pre-simulation gates: duplicate detection, novelty and the go/skip decision.

Everything here is deterministic — no LLM, no network — and every threshold
comes from :class:`~worldquant.registry.config.RegistryConfig`.

Three levels of duplication are reported independently:

* **Level 1 exact** — same canonical expression AND same normalized settings.
  Hard-blocked, no simulation quota is spent.
* **Level 2 template** — window-abstracted template (e.g.
  ``rank(ts_delta(close,<WINDOW>))``) already tried N times. Never blocked on
  its own; it feeds saturation + novelty.
* **Level 3 structure** — max operator/field/window similarity against the
  research set. Feeds the 0..100 novelty score.

The gate only rejects when BOTH hold: the template is saturated
(``max_template_trials``) AND novelty is below ``min_novelty``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .similarity import (
    jaccard,
    overall_similarity,
    structure_feature_similarity,
    window_set_similarity,
)
from .store import FactorRegistry, SimilarFactor

#: Trial counts at/above which a family/combination contributes ~zero novelty.
FAMILY_SATURATION_SCALE = 10.0
COMBINATION_SATURATION_SCALE = 10.0

#: Gate actions.
ACTION_SIMULATE = "SIMULATE"
ACTION_REJECT_EXACT = "REJECT_EXACT_DUPLICATE"
ACTION_SKIP_LOW_NOVELTY = "SKIP_LOW_NOVELTY"


@dataclass
class DuplicateCheckResult:
    exact_duplicate: bool
    template_duplicate: bool
    structure_similarity: float
    nearest_factors: list[SimilarFactor]
    template_trials: int = 0
    template: str = ""
    family: str | None = None
    exact_factor_id: int | None = None
    saturated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_duplicate": self.exact_duplicate,
            "exact_factor_id": self.exact_factor_id,
            "template_duplicate": self.template_duplicate,
            "template": self.template,
            "template_trials": self.template_trials,
            "saturated": self.saturated,
            "structure_similarity": self.structure_similarity,
            "family": self.family,
            "nearest_factors": [
                {
                    "factor_id": item.factor_id,
                    "expression": item.expression,
                    "status": item.status,
                    "similarity": round(item.similarity, 4),
                    "factor_family": item.factor_family,
                }
                for item in self.nearest_factors
            ],
        }


@dataclass
class NoveltyResult:
    score: float
    structure_novelty: float
    field_novelty: float
    family_novelty: float
    parameter_novelty: float
    combination_novelty: float
    nearest_factor_id: int | None
    nearest_similarity: float
    reason: str
    explanation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "structure_novelty": round(self.structure_novelty, 4),
            "field_novelty": round(self.field_novelty, 4),
            "family_novelty": round(self.family_novelty, 4),
            "parameter_novelty": round(self.parameter_novelty, 4),
            "combination_novelty": round(self.combination_novelty, 4),
            "nearest_factor_id": self.nearest_factor_id,
            "nearest_similarity": round(self.nearest_similarity, 4),
            "reason": self.reason,
            "weights": self.explanation.get("weights", {}),
            "family_trials": self.explanation.get("family_trials"),
            "combination_trials": self.explanation.get("combination_trials", {}),
        }


@dataclass
class PreSimulationGateResult:
    passed: bool
    action: str
    reason: str
    duplicate: DuplicateCheckResult
    novelty: NoveltyResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "action": self.action,
            "reason": self.reason,
            "duplicate": self.duplicate.to_dict(),
            "novelty": self.novelty.to_dict(),
        }


# --------------------------------------------------------------------------- #
# Internal: feature-level comparison against the prefiltered neighbour pool
# --------------------------------------------------------------------------- #
def _neighbor_component_sims(
    registry: FactorRegistry, fp: dict[str, Any]
) -> list[dict[str, Any]]:
    f = fp["features"]
    rows = registry.research_neighbors(fp)
    compared: list[dict[str, Any]] = []
    for row in rows:
        structure_equal = row["structure_hash"] == fp["structure_hash"]
        if structure_equal:
            struct_sim = 1.0
        else:
            struct_sim = structure_feature_similarity(
                set(f.operators), row["operators"],
                f.root_operator, row["feat_root"] or "",
                f.tree_depth, row["feat_depth"] or 0,
            )
        field_sim = jaccard(
            {name.lower() for name in f.fields},
            {name.lower() for name in row["fields_list"]},
        )
        window_sim = window_set_similarity(f.windows, row["windows_list"])
        overall = 0.5 * struct_sim + 0.3 * field_sim + 0.2 * window_sim
        compared.append(
            {
                "factor_id": row["id"],
                "expression": row["expression"],
                "status": row["status"],
                "factor_family": row["feat_family"],
                "brain_alpha_id": row["brain_alpha_id"],
                "struct": struct_sim,
                "field": field_sim,
                "window": window_sim,
                "overall": overall,
            }
        )
    compared.sort(key=lambda item: item["overall"], reverse=True)
    return compared


# --------------------------------------------------------------------------- #
# Level 1-3 duplicate check
# --------------------------------------------------------------------------- #
def duplicate_check(
    registry: FactorRegistry,
    expression: str,
    settings: dict[str, Any] | None = None,
) -> DuplicateCheckResult:
    fp = registry.fingerprints(expression, settings)

    exact_row = registry.find_exact(expression, settings)
    exact_id = int(exact_row["id"]) if exact_row else None

    trials = registry.template_trial_count(fp["template_hash"])
    max_trials = registry.config.pre_simulation.max_template_trials

    compared = _neighbor_component_sims(registry, fp)
    nearest = [
        SimilarFactor(
            factor_id=item["factor_id"],
            expression=item["expression"],
            status=item["status"],
            similarity=item["overall"],
            factor_family=item["factor_family"],
            brain_alpha_id=item["brain_alpha_id"],
        )
        for item in compared[:10]
    ]
    structure_similarity = compared[0]["overall"] if compared else 0.0

    return DuplicateCheckResult(
        exact_duplicate=exact_row is not None,
        template_duplicate=trials > 0,
        structure_similarity=round(structure_similarity, 4),
        nearest_factors=nearest,
        template_trials=trials,
        template=fp["features"].template,
        family=fp["factor_family"],
        exact_factor_id=exact_id,
        saturated=trials >= max_trials,
    )


# --------------------------------------------------------------------------- #
# Novelty
# --------------------------------------------------------------------------- #
def _family_trials(registry: FactorRegistry, family: str | None) -> int:
    if not family:
        return 0
    for row in registry.get_family_stats():
        if row.get("family") == family:
            return int(row.get("trials") or 0)
    return 0


def calculate_novelty(
    registry: FactorRegistry,
    expression: str,
    settings: dict[str, Any] | None = None,
) -> NoveltyResult:
    """Score research originality on a 0..100 scale with a full explanation."""
    fp = registry.fingerprints(expression, settings)
    f = fp["features"]
    weights = registry.config.novelty.normalized()

    compared = _neighbor_component_sims(registry, fp)
    if compared:
        nearest = compared[0]
        max_struct = max(item["struct"] for item in compared)
        max_field = max(item["field"] for item in compared)
        max_window = max(item["window"] for item in compared)
    else:
        nearest = None
        max_struct = max_field = max_window = 0.0

    structure_novelty = 1.0 - max_struct
    field_novelty = 1.0 - max_field
    parameter_novelty = 1.0 - max_window

    family = fp["factor_family"]
    family_trials = _family_trials(registry, family)
    family_novelty = math.exp(-family_trials / FAMILY_SATURATION_SCALE)

    combo_stats = {row["combination_key"]: row for row in registry.get_combination_stats()}
    combo_trials: dict[str, int] = {}
    combo_novelties: list[float] = []
    for combo in fp["combinations"]:
        trials = int(combo_stats.get(combo.key, {}).get("trial_count", 0))
        combo_trials[combo.key] = trials
        combo_novelties.append(math.exp(-trials / COMBINATION_SATURATION_SCALE))
    # No blend nodes -> nothing to remember; neutral-full novelty rather than
    # punishing every single-leg alpha for not using a combination.
    combination_novelty = sum(combo_novelties) / len(combo_novelties) if combo_novelties else 1.0

    score = 100.0 * (
        weights["structure"] * structure_novelty
        + weights["field"] * field_novelty
        + weights["family"] * family_novelty
        + weights["parameter"] * parameter_novelty
        + weights["combination"] * combination_novelty
    )
    score = round(max(0.0, min(100.0, score)), 2)

    reasons: list[str] = []
    if structure_novelty <= 0.05:
        reasons.append("operator structure already present in the research set")
    if field_novelty <= 0.05:
        reasons.append("uses the same data fields as existing factors")
    if parameter_novelty <= 0.2:
        reasons.append("window parameters fall inside already-tried horizons")
    if family_novelty <= 0.5:
        reasons.append(f"family {family} already has {family_trials} trials")
    if combo_novelties and min(combo_novelties) <= 0.5:
        reasons.append("one or more leg combinations are heavily explored")
    if not reasons:
        reasons.append("structure, fields and parameters are weakly covered")
    reason = "; ".join(reasons)

    return NoveltyResult(
        score=score,
        structure_novelty=structure_novelty,
        field_novelty=field_novelty,
        family_novelty=family_novelty,
        parameter_novelty=parameter_novelty,
        combination_novelty=combination_novelty,
        nearest_factor_id=nearest["factor_id"] if nearest else None,
        nearest_similarity=round(nearest["overall"], 4) if nearest else 0.0,
        reason=reason,
        explanation={
            "weights": weights,
            "family_trials": family_trials,
            "combination_trials": combo_trials,
            "template": f.template,
            "family": family,
        },
    )


# --------------------------------------------------------------------------- #
# Pre-simulation gate
# --------------------------------------------------------------------------- #
def pre_simulation_gate(
    registry: FactorRegistry,
    expression: str,
    settings: dict[str, Any] | None = None,
) -> PreSimulationGateResult:
    """Decide SIMULATE / REJECT_EXACT_DUPLICATE / SKIP_LOW_NOVELTY.

    Only exact duplicates are a hard block. A saturated template merely
    *contributes* to a skip, which additionally requires novelty below
    ``min_novelty`` — so a structurally familiar but meaningfully re-parameterized
    or re-fielded alpha is still allowed to run.
    """
    cfg = registry.config.pre_simulation
    duplicate = duplicate_check(registry, expression, settings)
    novelty = calculate_novelty(registry, expression, settings)

    if duplicate.exact_duplicate and cfg.reject_exact_duplicate:
        return PreSimulationGateResult(
            passed=False,
            action=ACTION_REJECT_EXACT,
            reason=(
                f"exact duplicate of factor#{duplicate.exact_factor_id} "
                "(same canonical expression + settings)"
            ),
            duplicate=duplicate,
            novelty=novelty,
        )

    if (
        cfg.reject_if_saturated_and_low_novelty
        and duplicate.saturated
        and novelty.score < cfg.min_novelty
    ):
        return PreSimulationGateResult(
            passed=False,
            action=ACTION_SKIP_LOW_NOVELTY,
            reason=(
                f"template already tried {duplicate.template_trials} times "
                f"(>= {cfg.max_template_trials}) and novelty {novelty.score:.1f} "
                f"< min_novelty {cfg.min_novelty:.1f}: {novelty.reason}"
            ),
            duplicate=duplicate,
            novelty=novelty,
        )

    return PreSimulationGateResult(
        passed=True,
        action=ACTION_SIMULATE,
        reason=(
            f"novelty {novelty.score:.1f}; template trials "
            f"{duplicate.template_trials}/{cfg.max_template_trials}"
        ),
        duplicate=duplicate,
        novelty=novelty,
    )
