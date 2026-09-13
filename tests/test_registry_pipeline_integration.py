"""Integration tests for the unified research pipeline (spec sections 30-35).

Covers, with no network access:

* three-layer identity semantics (expression / signal / experiment);
* ``gate_specs`` -> ``record_results`` -> ``evaluate_acceptance`` flow,
  including the research 0.65 correlation cutoff being strictly tighter than
  BRAIN's official 0.70 submission threshold;
* ablation sweep exemptions (same signal allowed, exact experiment blocked);
* orphan-neighbor reconciliation after a delayed import;
* clustering over real pairwise SELF edges only (SELF_MAX never joins nodes).
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from worldquant.api import SUBMISSION_CHECKS, SimulationStatus
from worldquant.hashing import (
    experiment_identity,
    expression_identity,
    signal_identity,
)
from worldquant.models import AlphaResult
from worldquant.registry import (
    CORR_TYPE_SELF,
    CORR_TYPE_SELF_MAX,
    FactorStatus,
    FactorRegistry,
    RegistryConfig,
    gate_specs,
    record_results,
    evaluate_acceptance,
    describe_corr_verdicts,
)
from worldquant.registry.clusters import get_clusters

EXPR = "rank(ts_delta(close, 5))"
SETTINGS = {"region": "USA", "universe": "TOP3000", "delay": 1}


@pytest.fixture
def registry(tmp_path):
    reg = FactorRegistry(tmp_path / "fr.db", RegistryConfig())
    yield reg
    reg.close()


def passing_checks():
    return {name: {"result": "PASS"} for name in SUBMISSION_CHECKS}


def pending_self_checks():
    checks = passing_checks()
    checks["SELF_CORRELATION"] = {"result": "PENDING"}
    return checks


def make_result(
    expression,
    *,
    settings=None,
    alpha_id="BRA-1",
    self_corr=None,
    neighbors=None,
    checks=None,
    grade="GOOD",
):
    """A metric-passing AlphaResult with controllable submission evidence."""
    settings = settings or SETTINGS
    return AlphaResult(
        alpha_id=f"local-{alpha_id}",
        expression=expression,
        dedup_key=experiment_identity(expression, settings),
        status=SimulationStatus.COMPLETED,
        remote_alpha_id=alpha_id,
        settings_json=json.dumps(settings),
        sharpe=1.6,
        fitness=1.3,
        turnover=0.2,
        returns=0.11,
        drawdown=-0.05,
        margin=0.001,
        long_count=120,
        short_count=110,
        grade=grade,
        passed=True,
        submission_checks=checks if checks is not None else passing_checks(),
        self_correlation=self_corr,
        self_correlated_with=neighbors,
    )


def gate_and_record(reg, expression, settings=None, **result_kwargs):
    """Run one full candidate -> result cycle, returning the outcome dict."""
    settings = settings or SETTINGS
    from worldquant.registry.adapter import gate_candidate, record_completed

    gate_candidate(reg, expression, settings)
    result = make_result(expression, settings=settings, **result_kwargs)
    outcome = record_completed(reg, result)
    return result, outcome


# --------------------------------------------------------------------------- #
# Spec 32: three-layer identity
# --------------------------------------------------------------------------- #
class TestIdentityLayers:
    def test_case_a_non_scope_settings_split_signal_from_experiment(self):
        # Same expression / region / universe / delay, different decay:
        # expression- and signal-identity match, experiment-identity differs.
        a = {**SETTINGS, "decay": 4}
        b = {**SETTINGS, "decay": 8}
        assert expression_identity(EXPR) == expression_identity(EXPR)
        assert signal_identity(EXPR, a) == signal_identity(EXPR, b)
        assert experiment_identity(EXPR, a) != experiment_identity(EXPR, b)

    def test_case_b_universe_is_part_of_signal_scope(self):
        other = {**SETTINGS, "universe": "TOP1000"}
        assert signal_identity(EXPR, SETTINGS) != signal_identity(EXPR, other)
        assert experiment_identity(EXPR, SETTINGS) != experiment_identity(
            EXPR, other
        )

    def test_case_c_delay_is_part_of_signal_scope(self):
        other = {**SETTINGS, "delay": 0}
        assert signal_identity(EXPR, SETTINGS) != signal_identity(EXPR, other)

    def test_case_d_identical_experiment_has_identical_identity(self):
        assert signal_identity(EXPR, dict(SETTINGS)) == signal_identity(
            EXPR, dict(SETTINGS)
        )
        assert experiment_identity(EXPR, dict(SETTINGS)) == experiment_identity(
            EXPR, dict(SETTINGS)
        )

    def test_different_expression_diverges_at_expression_layer(self):
        other = "rank(ts_delta(close, 10))"
        assert expression_identity(EXPR) != expression_identity(other)
        assert signal_identity(EXPR, SETTINGS) != signal_identity(other, SETTINGS)


# --------------------------------------------------------------------------- #
# Spec 30: gate -> record primitives
# --------------------------------------------------------------------------- #
class TestGateRecordPrimitives:
    def _spec(self, **overrides):
        kwargs = {"expression": EXPR, "settings": SETTINGS, "source": "generator"}
        kwargs.update(overrides)
        return SimpleNamespace(**kwargs)

    def test_registry_none_is_a_passthrough(self):
        spec = self._spec()
        batch = gate_specs(None, [spec])
        assert batch.kept == [spec]
        assert batch.blocked == 0
        result = make_result(EXPR)
        assert record_results(None, [result]) == [(result, None)]

    def test_exact_duplicate_is_gated_without_resimulation(self, registry):
        spec = self._spec()
        first = gate_specs(registry, [spec])
        assert first.blocked == 0 and len(first.kept) == 1

        simulated = []
        for kept in first.kept:  # fake "simulation"
            result, outcome = record_results(
                registry, [make_result(kept.expression, self_corr=0.30,
                                      neighbors=[{"alpha_id": "BRA-X",
                                                  "correlation": 0.30}])]
            )[0]
            simulated.append(kept)
        assert len(simulated) == 1

        second = gate_specs(registry, [self._spec()])
        assert second.kept == []
        assert second.tally.get("REJECT_EXACT_DUPLICATE") == 1

    def test_force_bypasses_exact_duplicate_gate(self, registry):
        gate_specs(registry, [self._spec()])
        forced = gate_specs(registry, [self._spec()], force=True)
        assert len(forced.kept) == 1

    def test_spec_level_force_gate_overrides_batch(self, registry):
        gate_specs(registry, [self._spec()])
        batch = gate_specs(registry, [self._spec(force_gate=True)])
        assert len(batch.kept) == 1

    def test_successful_result_is_persisted_as_passed(self, registry):
        result, outcome = gate_and_record(
            registry,
            EXPR,
            self_corr=0.30,
            neighbors=[{"alpha_id": "BRA-X", "correlation": 0.30}],
        )
        assert outcome["status"] == FactorStatus.PASSED
        decision = evaluate_acceptance(result, outcome, grade_ok=True)
        assert decision.accepted is True
        assert decision.research_corr_status == "PASS"
        assert decision.research_threshold == pytest.approx(0.65)
        assert decision.official_threshold == pytest.approx(0.70)

    def test_neighbors_are_stored_as_pairwise_self_edges(self, registry):
        _, outcome = gate_and_record(
            registry,
            EXPR,
            self_corr=0.30,
            neighbors=[{"alpha_id": "BRA-X", "correlation": 0.30}],
        )
        rows = registry._query(
            "SELECT correlation_type, other_brain_alpha_id, correlation "
            "FROM factor_correlations WHERE factor_id = ?",
            (outcome["factor_id"],),
        )
        types = {row["correlation_type"] for row in rows}
        assert CORR_TYPE_SELF in types
        assert CORR_TYPE_SELF_MAX not in types


# --------------------------------------------------------------------------- #
# Spec 31: research cutoff (0.65) is stricter than BRAIN official (0.70)
# --------------------------------------------------------------------------- #
class TestResearchCorrelationCutoff:
    def _evaluate(self, reg, *, self_corr, checks=None, neighbors=True,
                  allow_unknown=False):
        neighbor_rows = None
        if neighbors and self_corr is not None:
            neighbor_rows = [
                {"alpha_id": "BRA-X", "correlation": self_corr}
            ]
        result, outcome = gate_and_record(
            reg,
            EXPR,
            self_corr=self_corr,
            neighbors=neighbor_rows,
            checks=checks,
        )
        decision = evaluate_acceptance(
            result,
            outcome,
            grade_ok=True,
            allow_unknown=allow_unknown,
            corr_config=reg.config.correlation,
        )
        return result, outcome, decision

    def test_068_passes_brain_but_fails_research_gate(self, registry):
        result, outcome, decision = self._evaluate(registry, self_corr=0.68)
        # BRAIN: all eight checks (SELF_CORRELATION included) read PASS.
        assert result.is_submittable is True
        # Registry: factor rejected, research verdict FAIL at 0.65.
        assert outcome["status"] == FactorStatus.CORR_REJECTED
        assert decision.research_corr_status == "FAIL"
        assert decision.accepted is False
        assert decision.corr_margin is not None and decision.corr_margin < 0
        assert "research correlation FAIL" in decision.reason
        line = describe_corr_verdicts(result, outcome,
                                      corr_config=registry.config.correlation)
        assert "0.70" in line and "0.65" in line and "FAIL" in line

    def test_060_passes_both_gates(self, registry):
        result, outcome, decision = self._evaluate(registry, self_corr=0.60)
        assert result.is_submittable is True
        assert decision.research_corr_status == "PASS"
        assert decision.accepted is True

    def test_unknown_evidence_rejects_by_default(self, registry):
        # No neighbor rows and no aggregate: check never produced evidence.
        result, outcome, decision = self._evaluate(
            registry, self_corr=None, neighbors=False,
            checks=pending_self_checks(),
        )
        assert result.is_submittable is False
        assert outcome["status"] == FactorStatus.SIMULATED
        assert decision.research_corr_status == "UNKNOWN"
        assert decision.accepted is False
        assert "UNKNOWN" in decision.reason

    def test_unknown_evidence_only_accepted_when_explicitly_allowed(self, registry):
        result, outcome, decision = self._evaluate(
            registry, self_corr=None, neighbors=False, allow_unknown=True
        )
        assert result.is_submittable is True
        assert decision.research_corr_status == "UNKNOWN"
        assert decision.accepted is True

    def test_no_registry_outcome_requires_allow_unknown(self):
        result = make_result(EXPR, self_corr=None)
        rejected = evaluate_acceptance(result, None, grade_ok=True)
        assert rejected.accepted is False
        assert rejected.research_corr_status is None
        allowed = evaluate_acceptance(result, None, grade_ok=True,
                                      allow_unknown=True)
        assert allowed.accepted is True

    def test_bad_grade_rejects_even_when_correlation_passes(self, registry):
        result, outcome, decision = self._evaluate(registry, self_corr=0.40)
        decision = evaluate_acceptance(result, outcome, grade_ok=False,
                                       corr_config=registry.config.correlation)
        assert decision.accepted is False
        assert "grade" in decision.reason

    def test_disabled_gate_is_not_applicable_and_accepts_on_brain_checks(self, tmp_path):
        # correlation.enabled=false ⇒ NOT_APPLICABLE, never UNKNOWN: a
        # submittable metric-pass is accepted on BRAIN checks alone without
        # allow_unknown, and gets persisted PASSED/NOT_APPLICABLE.
        cfg = replace(
            RegistryConfig(),
            correlation=replace(RegistryConfig().correlation, enabled=False),
        )
        reg = FactorRegistry(tmp_path / "fr-off.db", cfg)
        try:
            result = make_result(EXPR, self_corr=0.68, alpha_id="BRA-OFF")
            outcome = record_results(reg, [result])[0][1]
            assert outcome["status"] == FactorStatus.PASSED
            assert outcome["corr_status"] == "NOT_APPLICABLE"
            decision = evaluate_acceptance(
                result, outcome, grade_ok=True, corr_config=cfg.correlation
            )
            assert decision.research_corr_status == "NOT_APPLICABLE"
            assert decision.accepted is True
            line = describe_corr_verdicts(
                result, outcome, corr_config=cfg.correlation
            )
            assert "NOT_APPLICABLE" in line
        finally:
            reg.close()

    def test_disabled_gate_still_refuses_when_brain_checks_fail(self, tmp_path):
        cfg = replace(
            RegistryConfig(),
            correlation=replace(RegistryConfig().correlation, enabled=False),
        )
        reg = FactorRegistry(tmp_path / "fr-off2.db", cfg)
        try:
            result = make_result(EXPR, self_corr=0.68, checks=passing_checks())
            outcome = record_results(reg, [result])[0][1]
            # Pretend the official checks do not pass: NOT_APPLICABLE must not
            # rescue an alpha BRAIN itself rejects.
            result.submission_checks["SELF_CORRELATION"] = {"result": "FAIL"}
            decision = evaluate_acceptance(
                result, outcome, grade_ok=True, corr_config=cfg.correlation
            )
            assert decision.accepted is False
            assert "BRAIN submission checks" in decision.reason
        finally:
            reg.close()

    def test_stored_failure_remains_a_failure_when_gate_is_disabled(self, tmp_path):
        # A historical FAIL verdict is real evidence: flipping the gate off
        # must not retroactively turn it into NOT_APPLICABLE.
        cfg = replace(
            RegistryConfig(),
            correlation=replace(RegistryConfig().correlation, enabled=False),
        )
        reg = FactorRegistry(tmp_path / "fr-off3.db", cfg)
        try:
            result = make_result(EXPR, self_corr=0.68, alpha_id="BRA-H")
            outcome = {
                "factor_id": 1,
                "status": FactorStatus.CORR_REJECTED,
                "corr_status": "FAIL",
                "correlation_decision": None,
            }
            decision = evaluate_acceptance(
                result, outcome, grade_ok=True, corr_config=cfg.correlation
            )
            assert decision.research_corr_status == "FAIL"
            assert decision.accepted is False
        finally:
            reg.close()

    def test_unknown_status_is_preserved_only_for_missing_evidence(self):
        # UNKNOWN means "check needed, evidence not yet obtained" — it must be
        # distinct from the disabled NOT_APPLICABLE state.
        result = make_result(EXPR, self_corr=None)
        outcome = {
            "factor_id": 1,
            "status": FactorStatus.SIMULATED,
            "corr_status": "UNKNOWN",
            "correlation_decision": None,
        }
        decision = evaluate_acceptance(
            result, outcome, grade_ok=True,
            corr_config=RegistryConfig().correlation,
        )
        assert decision.research_corr_status == "UNKNOWN"
        assert decision.accepted is False
        assert "UNKNOWN" in decision.reason

    # ------------------------------------------------------------------ #
    # Persisted corr_status on a SUBMITTED factor: the lifecycle fallback
    # must NOT be used, because SUBMITTED is neither PASSED nor SIMULATED.
    # Without the fix these all fall through to "not evaluated".
    # ------------------------------------------------------------------ #
    def test_submitted_with_stored_pass_is_accepted(self):
        result = make_result(EXPR, self_corr=0.40, alpha_id="BRA-P")
        outcome = {
            "factor_id": 1,
            "status": FactorStatus.SUBMITTED,
            "corr_status": "PASS",
            "correlation_decision": None,
        }
        decision = evaluate_acceptance(
            result, outcome, grade_ok=True,
            corr_config=RegistryConfig().correlation,
        )
        assert decision.research_corr_status == "PASS"
        assert decision.accepted is True

    def test_submitted_with_stored_fail_is_rejected(self):
        result = make_result(EXPR, self_corr=0.68, alpha_id="BRA-F")
        outcome = {
            "factor_id": 1,
            "status": FactorStatus.SUBMITTED,
            "corr_status": "FAIL",
            "correlation_decision": None,
        }
        decision = evaluate_acceptance(
            result, outcome, grade_ok=True,
            corr_config=RegistryConfig().correlation,
        )
        assert decision.research_corr_status == "FAIL"
        assert decision.accepted is False
        assert "research correlation FAIL" in decision.reason

    def test_submitted_with_stored_unknown_rejects_by_default(self):
        result = make_result(EXPR, self_corr=None, alpha_id="BRA-U")
        outcome = {
            "factor_id": 1,
            "status": FactorStatus.SUBMITTED,
            "corr_status": "UNKNOWN",
            "correlation_decision": None,
        }
        decision = evaluate_acceptance(
            result, outcome, grade_ok=True,
            corr_config=RegistryConfig().correlation,
        )
        assert decision.research_corr_status == "UNKNOWN"
        assert decision.accepted is False
        assert "UNKNOWN" in decision.reason
        # Explicit allow_unknown still rescues a genuinely-unknown historical alpha.
        allowed = evaluate_acceptance(
            result, outcome, grade_ok=True, allow_unknown=True,
            corr_config=RegistryConfig().correlation,
        )
        assert allowed.accepted is True

    def test_submitted_with_stored_not_applicable_uses_brain_checks(self):
        # NOT_APPLICABLE means the local gate was off when this factor was
        # recorded: acceptance depends solely on BRAIN's official checks.
        result = make_result(EXPR, self_corr=0.68, alpha_id="BRA-NA")
        outcome = {
            "factor_id": 1,
            "status": FactorStatus.SUBMITTED,
            "corr_status": "NOT_APPLICABLE",
            "correlation_decision": None,
        }
        decision = evaluate_acceptance(
            result, outcome, grade_ok=True,
            corr_config=RegistryConfig().correlation,
        )
        assert decision.research_corr_status == "NOT_APPLICABLE"
        assert decision.accepted is True
        # Brain-check failure is still rejected even with NOT_APPLICABLE.
        result.submission_checks["SELF_CORRELATION"] = {"result": "FAIL"}
        rejected = evaluate_acceptance(
            result, outcome, grade_ok=True,
            corr_config=RegistryConfig().correlation,
        )
        assert rejected.accepted is False
        assert "BRAIN submission checks" in rejected.reason


# --------------------------------------------------------------------------- #
# Spec 33: ablation sweeps
# --------------------------------------------------------------------------- #
class TestAblationSweeps:
    @pytest.fixture
    def sweep_registry(self, tmp_path):
        base = RegistryConfig()
        cfg = replace(
            base,
            pre_simulation=replace(base.pre_simulation, max_signal_experiments=1),
        )
        reg = FactorRegistry(tmp_path / "sweep.db", cfg)
        yield reg
        reg.close()

    @staticmethod
    def _spec(decay, source="generator"):
        return SimpleNamespace(
            expression=EXPR,
            settings={**SETTINGS, "decay": decay},
            source=source,
            force_gate=False,
            ablation_group_id="g1" if source == "ablation" else None,
            changed_parameters={"decay": decay} if source == "ablation" else None,
        )

    def test_normal_same_signal_sweep_is_blocked_at_capacity(self, sweep_registry):
        from worldquant.registry.adapter import gate_candidate, record_completed

        gate_candidate(sweep_registry, EXPR, {**SETTINGS, "decay": 4})
        record_completed(
            sweep_registry,
            make_result(EXPR, settings={**SETTINGS, "decay": 4}, alpha_id="BRA-A",
                        self_corr=0.2,
                        neighbors=[{"alpha_id": "BRA-N", "correlation": 0.2}]),
        )
        batch = gate_specs(sweep_registry, [self._spec(8)])
        assert batch.kept == []
        assert batch.tally.get("SKIP_SIGNAL_DUPLICATE") == 1

    def test_ablation_same_signal_sweep_is_allowed(self, sweep_registry):
        from worldquant.registry.adapter import gate_candidate, record_completed

        gate_candidate(sweep_registry, EXPR, {**SETTINGS, "decay": 4})
        record_completed(
            sweep_registry,
            make_result(EXPR, settings={**SETTINGS, "decay": 4}, alpha_id="BRA-A",
                        self_corr=0.2,
                        neighbors=[{"alpha_id": "BRA-N", "correlation": 0.2}]),
        )
        batch = gate_specs(sweep_registry, [self._spec(8, source="ablation")])
        assert len(batch.kept) == 1
        assert batch.blocked == 0

    def test_ablation_still_blocks_exact_experiment_duplicate(self, sweep_registry):
        from worldquant.registry.adapter import gate_candidate, record_completed

        settings8 = {**SETTINGS, "decay": 8}
        gate_candidate(
            sweep_registry, EXPR, settings8, source="ablation"
        )
        # The first ablation experiment is actually researched; a second
        # attempt with the identical experiment is an exact duplicate.
        record_completed(
            sweep_registry,
            make_result(EXPR, settings=settings8, alpha_id="BRA-B",
                        self_corr=0.2,
                        neighbors=[{"alpha_id": "BRA-N", "correlation": 0.2}]),
        )
        batch = gate_specs(
            sweep_registry, [self._spec(8, source="ablation")]
        )
        assert batch.kept == []
        assert batch.tally.get("REJECT_EXACT_DUPLICATE") == 1


# --------------------------------------------------------------------------- #
# Spec 34: orphan-neighbor reconciliation
# --------------------------------------------------------------------------- #
class TestCorrelationReconciliation:
    def test_orphan_edge_resolved_after_neighbor_import(self, registry):
        # A simulated first, neighbor BRA-X is not yet a local factor.
        factor_a = registry.register_candidate("rank(close)", SETTINGS).factor_id
        written = registry.save_correlations(
            factor_a,
            [{"alpha_id": "BRA-X", "correlation": 0.82}],
            CORR_TYPE_SELF,
        )
        assert written == 1
        before = registry._query(
            "SELECT other_factor_id FROM factor_correlations "
            "WHERE factor_id = ? AND correlation_type = ?",
            (factor_a, CORR_TYPE_SELF),
        )
        assert len(before) == 1 and before[0]["other_factor_id"] is None

        # Neighbor imported later with its BRAIN id.
        factor_b = registry.register_candidate(
            "rank(open)", SETTINGS, brain_alpha_id="BRA-X"
        ).factor_id
        summary = registry.reconcile_correlation_neighbors()
        assert summary["resolved"] == 1

        after = registry._query(
            "SELECT other_factor_id FROM factor_correlations "
            "WHERE factor_id = ? AND correlation_type = ?",
            (factor_a, CORR_TYPE_SELF),
        )
        assert after[0]["other_factor_id"] == factor_b

        # Idempotent: running again resolves nothing and duplicates nothing.
        again = registry.reconcile_correlation_neighbors()
        assert again["resolved"] == 0
        count = registry._query(
            "SELECT COUNT(*) AS n FROM factor_correlations "
            "WHERE factor_id = ? AND correlation_type = ?",
            (factor_a, CORR_TYPE_SELF),
        )
        assert count[0]["n"] == 1


# --------------------------------------------------------------------------- #
# Spec 35: clustering uses pairwise SELF edges only
# --------------------------------------------------------------------------- #
class TestCorrelationClusters:
    @staticmethod
    def _seed_factor(registry, brain_id, expression):
        return registry.register_candidate(
            expression, SETTINGS, brain_alpha_id=brain_id
        ).factor_id

    def test_self_edges_form_one_cluster_self_max_never_merges(self, registry):
        a = self._seed_factor(registry, "BRA-A", "rank(close)")
        b = self._seed_factor(registry, "BRA-B", "rank(open)")
        c = self._seed_factor(registry, "BRA-C", "rank(high)")
        d = self._seed_factor(registry, "BRA-D", "rank(low)")

        registry.save_correlations(
            a, [{"alpha_id": "BRA-B", "correlation": 0.82}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            b, [{"alpha_id": "BRA-C", "correlation": 0.75}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            a, [{"alpha_id": "BRA-C", "correlation": 0.79}], CORR_TYPE_SELF
        )
        # D only has an aggregate SELF_MAX: it must not join the graph.
        registry.save_correlations(
            d, [{"correlation": 0.90, "other_brain_alpha_id": None}],
            CORR_TYPE_SELF_MAX,
        )

        clusters = sorted(
            get_clusters(registry, threshold=0.70, min_size=1),
            key=lambda cluster: cluster.size,
            reverse=True,
        )
        big = clusters[0]
        assert big.size == 3
        assert set(big.factor_ids) == {a, b, c}

        singleton = next(
            cluster for cluster in clusters if d in cluster.factor_ids
        )
        assert singleton.size == 1

    def test_edges_below_threshold_do_not_merge(self, registry):
        a = self._seed_factor(registry, "BRA-A", "rank(close)")
        b = self._seed_factor(registry, "BRA-B", "rank(open)")
        registry.save_correlations(
            a, [{"alpha_id": "BRA-B", "correlation": 0.55}], CORR_TYPE_SELF
        )
        clusters = get_clusters(registry, threshold=0.70, min_size=1)
        assert all(cluster.size == 1 for cluster in clusters)

    def test_chain_with_weak_end_to_end_link_is_not_saturated(self, registry):
        # A-B=0.71, B-C=0.71 merge all three into one graph cluster, but the
        # observed A-C=0.20 weak link must keep unbiased cluster stats honest:
        # mean |corr| over ALL observed pairs is 0.54, not the graph-edge-only
        # 0.71 average; density is 2/3; neither clears the saturation rule.
        a = self._seed_factor(registry, "BRA-A", "rank(close)")
        b = self._seed_factor(registry, "BRA-B", "rank(open)")
        c = self._seed_factor(registry, "BRA-C", "rank(high)")
        registry.save_correlations(
            a, [{"alpha_id": "BRA-B", "correlation": 0.71}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            b, [{"alpha_id": "BRA-C", "correlation": 0.71}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            a, [{"alpha_id": "BRA-C", "correlation": 0.20}], CORR_TYPE_SELF
        )

        clusters = get_clusters(registry, threshold=0.70, min_size=1)
        big = max(clusters, key=lambda cluster: cluster.size)
        assert set(big.factor_ids) == {a, b, c}

        # Legacy graph-edge statistic stays >= cutoff (kept for compatibility).
        assert big.avg_corr is not None
        assert big.avg_corr == pytest.approx(0.71, abs=1e-9)
        # The unbiased statistics must not repeat that bias.
        assert big.mean_abs_corr == pytest.approx(
            (0.71 + 0.71 + 0.20) / 3.0, abs=1e-9
        )
        assert big.total_pair_count == 3
        assert big.known_pair_count == 3
        assert big.known_pair_coverage == pytest.approx(1.0)
        assert big.high_corr_density == pytest.approx(2.0 / 3.0)
        assert big.max_corr == pytest.approx(0.71)
        assert big.saturated is False
        payload = big.to_dict()
        assert payload["mean_abs_corr"] == big.mean_abs_corr
        assert payload["known_pair_coverage"] == big.known_pair_coverage
        assert payload["high_corr_density"] == big.high_corr_density

    def test_unobserved_pairs_lower_coverage_and_density_not_mean(self, registry):
        # 4 members, only three 0.90 chain links observed, three pairs have no
        # row at all. They must NOT be treated as zero correlation: mean stays
        # 0.90 while coverage/density are both 0.5 and the cluster is not
        # saturated (coverage below the default 0.60 requirement).
        a = self._seed_factor(registry, "BRA-A", "rank(close)")
        b = self._seed_factor(registry, "BRA-B", "rank(open)")
        c = self._seed_factor(registry, "BRA-C", "rank(high)")
        d = self._seed_factor(registry, "BRA-D", "rank(low)")
        registry.save_correlations(
            a, [{"alpha_id": "BRA-B", "correlation": 0.90}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            b, [{"alpha_id": "BRA-C", "correlation": 0.90}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            c, [{"alpha_id": "BRA-D", "correlation": 0.90}], CORR_TYPE_SELF
        )

        clusters = get_clusters(registry, threshold=0.70, min_size=1)
        big = max(clusters, key=lambda cluster: cluster.size)
        assert set(big.factor_ids) == {a, b, c, d}
        assert big.total_pair_count == 6
        assert big.known_pair_count == 3
        assert big.known_pair_coverage == pytest.approx(0.5)
        assert big.high_corr_density == pytest.approx(0.5)
        assert big.mean_abs_corr == pytest.approx(0.90)
        assert big.avg_corr == pytest.approx(0.90)
        assert big.saturated is False
