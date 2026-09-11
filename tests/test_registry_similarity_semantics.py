"""Semantic tests for the v2 similarity / identity engine.

These cases encode the operational lessons from real research:

* the same signal with window 20 vs 21 is essentially the same experiment;
* ``close`` vs ``vwap`` stays similar at the field-*family* layer;
* a different data family must open clear novelty headroom;
* operator root-to-leaf paths distinguish differently nested transforms;
* blend identity follows the math (A+B == B+A, A-B != B-A);
* window proximity is graded rather than treated as equal/unknown.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from worldquant.api import SimulationStatus
from worldquant.registry import FactorRegistry, build_generation_context
from worldquant.registry.config import PreSimulationConfig, RegistryConfig
from worldquant.registry.expr_parser import analyze_expression
from worldquant.registry.similarity import (
    jaccard,
    structure_feature_similarity,
    window_set_similarity,
)
from worldquant.registry.store import CORR_TYPE_SELF

SETTINGS = {"region": "USA", "universe": "TOP3000", "delay": 1}
ANALYST_FIELD = "anl4_fs_detail_estimates_advanced_af_nd_fcf_mean"


@pytest.fixture()
def registry(tmp_path):
    with FactorRegistry(tmp_path / "fr.db", RegistryConfig()) as reg:
        yield reg


def _completed(
    expression: str,
    alpha_id: str | None = None,
    *,
    sharpe: float = 1.4,
    fitness: float = 1.1,
) -> SimpleNamespace:
    return SimpleNamespace(
        expression=expression,
        settings_json=json.dumps(SETTINGS, sort_keys=True),
        status=SimulationStatus.COMPLETED,
        remote_alpha_id=alpha_id,
        simulation_id=f"sim-{alpha_id}" if alpha_id else None,
        registry_source="generator",
        sharpe=sharpe,
        fitness=fitness,
        turnover=0.3,
        returns=0.1,
        drawdown=-0.08,
        margin=0.001,
        long_count=100,
        short_count=100,
        grade="GOOD",
        passed=True,
        reasons=[],
        submission_checks={},
        checks={},
        test_stats=None,
        error=None,
    )


def simulate(
    registry: FactorRegistry, expression: str, alpha_id: str | None = None
) -> int:
    registry.register_candidate(expression, SETTINGS)
    return registry.save_simulation_result(_completed(expression, alpha_id))


# --------------------------------------------------------------------------- #
# Case A/B/C/D: graded field-family novelty
# --------------------------------------------------------------------------- #
class TestFieldFamilyLayering:
    def test_close20_vs_close21_is_nearly_identical(self, registry):
        seed = "rank(ts_delta(close,20))"
        simulate(registry, seed)
        verdict = registry.evaluate_candidate("rank(ts_delta(close,21))", SETTINGS)
        # Distinct windows, distinct signal — but the experiment is a trivial
        # parameter sweep: nearest similarity is extremely high, novelty low.
        assert verdict["nearest_similarity"] == pytest.approx(1.0, abs=0.15)
        assert verdict["novelty"]["score"] < 40.0

    def test_close_vs_vwap_stays_similar_at_family_layer(self, registry):
        simulate(registry, "rank(ts_delta(close,20))")
        same_window = registry.evaluate_candidate(
            "rank(ts_delta(close,21))", SETTINGS
        )
        family_swap = registry.evaluate_candidate(
            "rank(ts_delta(vwap,20))", SETTINGS
        )
        # Same concrete field is closer than a family-internal swap ...
        assert (
            same_window["nearest_similarity"]
            > family_swap["nearest_similarity"]
        )
        # ... but close/vwap both sit in PRICE, so this is still a LOW-novelty
        # variant (family layer keeps similarity well above a cross-family one).
        assert family_swap["nearest_similarity"] > 0.70
        assert family_swap["novelty"]["score"] < 50.0

    def test_price_vs_analyst_opens_more_headroom(self, registry):
        simulate(registry, "rank(ts_delta(close,20))")
        vwap_variant = registry.evaluate_candidate(
            "rank(ts_delta(vwap,20))", SETTINGS
        )
        analyst_variant = registry.evaluate_candidate(
            f"rank(ts_delta({ANALYST_FIELD},20))", SETTINGS
        )
        assert (
            analyst_variant["nearest_similarity"]
            < vwap_variant["nearest_similarity"]
        )
        assert analyst_variant["novelty"]["score"] > vwap_variant["novelty"]["score"]

    def test_price_reversal_vs_analyst_earnings_is_high_novelty(self, registry):
        simulate(registry, "group_rank(ts_zscore(close,60),subindustry)")
        fresh = registry.evaluate_candidate(
            f"ts_delta({ANALYST_FIELD},5)", SETTINGS
        )
        assert fresh["nearest_similarity"] < 0.55
        assert fresh["novelty"]["score"] > 65.0
        assert fresh["action"] == "SIMULATE"


# --------------------------------------------------------------------------- #
# Operator paths preserve nesting order
# --------------------------------------------------------------------------- #
class TestOperatorPaths:
    def test_reordered_nesting_is_distinguished(self):
        nested = "rank(ts_mean(ts_delta(close,5),10))"
        reordered = "ts_mean(rank(ts_delta(close,5)),10)"
        a = analyze_expression(nested)
        b = analyze_expression(reordered)
        # Same operator multiset ...
        assert a.operator_multiset == b.operator_multiset
        assert a.tree_depth == b.tree_depth
        # ... different root + different root->leaf path set ...
        assert a.root_operator != b.root_operator
        assert set(a.operator_paths) != set(b.operator_paths)
        assert jaccard(set(a.operator_paths), set(b.operator_paths)) < 1.0
        # ... hence different structure fingerprints.
        assert a.structure != b.structure

    def test_path_component_lowers_similarity_for_reordered_nesting(self):
        nested = analyze_expression("rank(ts_mean(ts_delta(close,5),10))")
        reordered = analyze_expression("ts_mean(rank(ts_delta(close,5)),10)")
        score = structure_feature_similarity(
            set(nested.operators), set(reordered.operators),
            nested.root_operator, reordered.root_operator,
            nested.tree_depth, reordered.tree_depth,
            multiset_a=nested.operator_multiset,
            multiset_b=reordered.operator_multiset,
            paths_a=nested.operator_paths,
            paths_b=reordered.operator_paths,
        )
        assert score < 1.0
        # Identical expressions must trivially compare as equal.
        assert structure_feature_similarity(
            set(nested.operators), set(nested.operators),
            nested.root_operator, nested.root_operator,
            nested.tree_depth, nested.tree_depth,
            multiset_a=nested.operator_multiset,
            multiset_b=nested.operator_multiset,
            paths_a=nested.operator_paths,
            paths_b=nested.operator_paths,
        ) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Windows are graded, not boolean
# --------------------------------------------------------------------------- #
class TestWindowGraduation:
    def test_close_windows_are_similar_far_windows_are_not(self):
        near = window_set_similarity([20], [21])
        far = window_set_similarity([20], [252])
        assert near > 0.30
        assert far < near
        assert far < 0.15


# --------------------------------------------------------------------------- #
# Combination leg identity follows the arithmetic
# --------------------------------------------------------------------------- #
class TestCombinationLegHashes:
    def _combo(self, registry: FactorRegistry, expression: str, operator: str):
        combos = registry.fingerprints(expression, SETTINGS)["combinations"]
        matches = [c for c in combos if c.operator == operator]
        assert matches, f"no {operator} combination found in {expression}"
        return matches[0]

    def test_addition_is_order_invariant(self, registry):
        ab = self._combo(registry, "rank(close)+rank(volume)", "ADD")
        ba = self._combo(registry, "rank(volume)+rank(close)", "ADD")
        assert ab.key == ba.key
        assert ab.structure_hash == ba.structure_hash

    def test_subtraction_is_order_sensitive(self, registry):
        ab = self._combo(registry, "rank(close)-rank(volume)", "SUBTRACT")
        ba = self._combo(registry, "rank(volume)-rank(close)", "SUBTRACT")
        assert ab.key != ba.key
        assert ab.structure_hash != ba.structure_hash
        # rank(...) over a price field is themed PRICE_REVERSAL; the point is
        # that the ordered sides swap.
        assert (ab.left_leg or {}).get("family") == "PRICE_REVERSAL"
        assert (ab.right_leg or {}).get("family") == "LIQUIDITY"
        assert (ba.left_leg or {}).get("family") == "LIQUIDITY"
        assert (ba.right_leg or {}).get("family") == "PRICE_REVERSAL"


# --------------------------------------------------------------------------- #
# L1 signal identity: same expression, settings variants
# --------------------------------------------------------------------------- #
class TestSignalIdentity:
    def _registry_with_signal_cap(self, tmp_path, cap: int) -> FactorRegistry:
        config = RegistryConfig(
            pre_simulation=PreSimulationConfig(max_signal_experiments=cap)
        )
        return FactorRegistry(tmp_path / "fr.db", config)

    def test_exact_settings_are_experiment_duplicate(self, registry):
        simulate(registry, "rank(close)")
        verdict = registry.evaluate_candidate("rank(close)", SETTINGS)
        assert verdict["experiment_duplicate"] is True
        assert verdict["action"] == "REJECT_EXACT_DUPLICATE"
        assert verdict["signal_trials"] >= 1

    def test_settings_variants_share_signal_identity(self, registry):
        simulate(registry, "rank(close)")
        variant_settings = {**SETTINGS, "truncation": 0.05}
        verdict = registry.evaluate_candidate("rank(close)", variant_settings)
        assert verdict["experiment_duplicate"] is False
        assert verdict["same_signal"] is True
        assert verdict["signal_trials"] == 1
        assert verdict["previous_experiments"]
        # Even one prior parameter variant already discounts novelty.
        assert verdict["novelty"]["score"] < 40.0

    def test_signal_duplicate_skipped_after_cap_but_ablation_exempt(self, tmp_path):
        registry = self._registry_with_signal_cap(tmp_path, 2)
        with registry:
            simulate(registry, "rank(close)")
            registry.save_simulation_result(
                _completed_variant("rank(close)", {"truncation": 0.05})
            )
            third = registry.evaluate_candidate(
                "rank(close)", {**SETTINGS, "truncation": 0.10}
            )
            assert third["action"] == "SKIP_SIGNAL_DUPLICATE"
            ablation = registry.evaluate_candidate(
                "rank(close)", {**SETTINGS, "truncation": 0.10},
                source="ablation",
            )
            assert ablation["action"] == "SIMULATE"
            assert ablation["ablation"]["is_ablation"] is True

    def test_force_overrides_hard_reject(self, registry):
        simulate(registry, "rank(close)")
        verdict = registry.evaluate_candidate("rank(close)", SETTINGS, force=True)
        assert verdict["action"] == "SIMULATE"


def _completed_variant(expression: str, extra_settings: dict) -> SimpleNamespace:
    settings = {**SETTINGS, **extra_settings}
    result = _completed(expression)
    result.settings_json = json.dumps(settings, sort_keys=True)
    return result


# --------------------------------------------------------------------------- #
# High-quality redundancy and saturated clusters
# --------------------------------------------------------------------------- #
class TestHighQualityRedundancy:
    def test_strong_rejected_factor_is_flagged_and_enters_context(self, registry):
        f0 = simulate(registry, "rank(ts_mean(close,22))", "live-hq-0")
        registry.mark_submitted(f0, brain_alpha_id="live-hq-0")
        f1 = registry.save_simulation_result(
            _completed("rank(ts_mean(close,23))", "live-hq-1",
                       sharpe=1.7, fitness=1.6)
        )
        registry.save_correlations(
            f1,
            [{"correlation": 0.84, "other_brain_alpha_id": "live-hq-0"}],
            CORR_TYPE_SELF,
        )
        f2 = registry.save_simulation_result(
            _completed("rank(ts_mean(close,24))", "live-hq-2",
                       sharpe=1.55, fitness=1.51)
        )
        registry.save_correlations(
            f2,
            [{"correlation": 0.83, "other_brain_alpha_id": "live-hq-1"}],
            CORR_TYPE_SELF,
        )

        decision = registry.apply_correlation_gate(f1)
        assert decision.passed is False
        assert registry.get_factor(f1)["status"] == "CORR_REJECTED"
        redundant = registry.get_high_quality_redundant()
        assert [row["id"] for row in redundant] == [f1]
        assert redundant[0]["sharpe"] >= 1.5
        assert redundant[0]["fitness"] >= 1.5

        ctx = build_generation_context(registry)
        examples = ctx["high_quality_redundant_examples"]
        assert examples and examples[0]["factor_id"] == f1
        assert "switch legs/dataset" in examples[0]["advice"]

        # Size-3 cluster with 0.83+ internal corr is saturated -> DO_NOT_REPEAT.
        saturated = ctx["saturated_clusters"]
        assert saturated and saturated[0]["size"] == 3
        assert saturated[0]["avg_corr"] >= 0.70
        avoid_kinds = {item["kind"] for item in ctx["do_not_repeat"]}
        assert "cluster" in avoid_kinds
