"""Factor Registry / Alpha Memory System — full mock coverage.

No test here touches the network. Client pagination is driven by
:class:`conftest.FakeSession`; historical migration seeds a throwaway
``worldquant.db`` through the real :class:`ResultStore`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import FakeResponse, FakeSession, make_config, submission_checks
from worldquant.api import SimulationStatus
from worldquant.config import load_config
from worldquant.exceptions import ConfigError
from worldquant.hashing import normalize_expression
from worldquant.models import AlphaResult
from worldquant.registry import (
    CorrelationGate,
    FactorRegistry,
    FactorStatus,
    build_generation_context,
    build_registry_config,
    calculate_novelty,
    duplicate_check,
    gate_candidate,
    get_cluster_representatives,
    get_clusters,
    import_submitted_factors,
    migrate_existing_results,
    open_registry,
    pre_simulation_gate,
    quality_score,
    record_completed,
    research_priority,
)
from worldquant.registry.config import (
    CorrelationConfig,
    PreSimulationConfig,
    RegistryConfig,
)
from worldquant.registry.store import CORR_TYPE_SELF
from worldquant.storage import ResultStore, utcnow_iso

SETTINGS = {"region": "USA", "universe": "TOP3000", "delay": 1}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def registry(tmp_path):
    with FactorRegistry(tmp_path / "factor_registry.db", RegistryConfig()) as reg:
        yield reg


def make_completed(
    expression: str,
    alpha_id: str,
    *,
    sharpe: float = 1.5,
    fitness: float = 1.2,
    passed: bool | None = True,
    reasons=None,
    self_correlation=None,
    submission_checks_payload=None,
    status: str = SimulationStatus.COMPLETED,
    grade: str = "GOOD",
) -> SimpleNamespace:
    return SimpleNamespace(
        expression=expression,
        settings_json=json.dumps(SETTINGS, sort_keys=True),
        status=status,
        remote_alpha_id=alpha_id,
        simulation_id=f"sim-{alpha_id}",
        sharpe=sharpe,
        fitness=fitness,
        turnover=0.3,
        returns=0.1,
        drawdown=-0.08,
        margin=0.001,
        long_count=100,
        short_count=100,
        grade=grade,
        passed=passed,
        reasons=list(reasons or []),
        submission_checks=submission_checks_payload,
        test_stats=None,
        checks={},
        self_correlation=self_correlation,
        error=None,
    )


def simulate(registry, expression: str, alpha_id: str, **kwargs) -> int:
    registry.register_candidate(expression, SETTINGS)
    return registry.save_simulation_result(make_completed(expression, alpha_id, **kwargs))


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #
class TestFingerprints:
    def test_whitespace_and_case_normalization(self, registry):
        raw = "rank(  ts_delta( Close , 5) )"
        fp = registry.fingerprints(raw, SETTINGS)
        assert fp["canonical"] == "rank(ts_delta(Close,5))"
        # normalize_expression folds internal whitespace but preserves case.
        assert normalize_expression("a+b\n  +c") == "a+b+c"
        fp2 = registry.fingerprints("rank(ts_delta( Close ,5))", SETTINGS)
        assert fp["exact_hash"] == fp2["exact_hash"]

    def test_same_expression_different_settings_differ_in_exact_only(self, registry):
        a = registry.fingerprints("rank(close)", SETTINGS)
        b = registry.fingerprints(
            "rank(close)", {"region": "USA", "universe": "TOP1000", "delay": 1}
        )
        assert a["exact_hash"] != b["exact_hash"]
        assert a["structure_hash"] == b["structure_hash"]
        assert a["template_hash"] != b["template_hash"]  # scope is part of template hash

    def test_template_collapses_window_literals(self, registry):
        windows = [5, 10, 20, 60]
        templates = {
            registry.fingerprints(f"rank(ts_delta(close,{n}))", SETTINGS)["features"].template
            for n in windows
        }
        assert templates == {"rank(ts_delta(close,<WINDOW>))"}
        # Different field -> different template.
        other = registry.fingerprints("rank(ts_delta(vwap,5))", SETTINGS)
        assert other["features"].template == "rank(ts_delta(vwap,<WINDOW>))"
        # Field-level rendering abstracts the concrete field name away.
        assert registry.fingerprints("rank(ts_delta(close,5))", SETTINGS)[
            "features"
        ].field_template == "rank(ts_delta(<FIELD>,<WINDOW>))"

    def test_operator_and_field_extraction(self, registry):
        fp = registry.fingerprints(
            "group_rank(ts_zscore(anl4_ebit_value, 60), subindustry)", SETTINGS
        )
        features = fp["features"]
        assert "ts_zscore" in features.operators
        assert "group_rank" in features.operators
        assert features.fields == ["anl4_ebit_value"]
        assert features.windows == [60]
        assert features.root_operator == "group_rank"
        assert features.tree_depth >= 2

    def test_multi_statement_locals_are_not_fields(self, registry):
        recipe = (
            "ey = ts_backfill(anl4_ebit_value,40)/cap; "
            "group_zscore(ey + rank(ts_delta(close,5)), subindustry)"
        )
        fp = registry.fingerprints(recipe, SETTINGS)
        fields = set(fp["features"].fields)
        assert "ey" not in fields
        assert {"anl4_ebit_value", "cap", "close"} <= fields
        assert "subindustry" not in fields

    def test_factor_family_classification(self, registry):
        assert registry.fingerprints("rank(ts_delta(close,5))", SETTINGS)["factor_family"]
        analyst = registry.fingerprints(
            "rank(ts_backfill(anl4_ebit_value,40)/cap)", SETTINGS
        )
        assert analyst["factor_family"] == "ANALYST"

    def test_quoted_strings_survive_tokenizer(self, registry):
        fp = registry.fingerprints('trade_when(volume > 100000, rank(close), -1, "BOTH")',
                                   SETTINGS)
        assert fp["exact_hash"]
        assert "BOTH" not in fp["features"].fields

    def test_parser_fallback_on_bizarre_input(self, registry):
        # Analysis must never raise on malformed expressions: it falls back to
        # a conservative hash/feature set.
        fp = registry.fingerprints("rank(ts_delta(close,", SETTINGS)
        assert fp["exact_hash"]
        assert fp["structure_hash"]


# --------------------------------------------------------------------------- #
# Combinations
# --------------------------------------------------------------------------- #
class TestCombinations:
    def test_addition_and_multiplication_are_commutative(self, registry):
        a = registry.fingerprints("ts_rank(returns,5)+rank(volume)", SETTINGS)
        b = registry.fingerprints("rank(volume) + ts_rank(returns,5)", SETTINGS)
        assert a["combinations"] and a["combinations"][0].key == b["combinations"][0].key
        c = registry.fingerprints("rank(close)*rank(volume)", SETTINGS)
        d = registry.fingerprints("rank(volume)*rank(close)", SETTINGS)
        assert c["combinations"][0].key == d["combinations"][0].key

    def test_subtraction_and_division_keep_order(self, registry):
        a = registry.fingerprints("ts_rank(returns,5)-rank(volume)", SETTINGS)
        b = registry.fingerprints("rank(volume)-ts_rank(returns,5)", SETTINGS)
        assert a["combinations"][0].key != b["combinations"][0].key
        c = registry.fingerprints("rank(close)/rank(volume)", SETTINGS)
        d = registry.fingerprints("rank(volume)/rank(close)", SETTINGS)
        assert c["combinations"][0].key != d["combinations"][0].key


# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #
class TestSimilarity:
    def test_nearby_windows_outrank_distant_windows(self, registry):
        simulate(registry, "rank(ts_delta(close,21))", "a21")
        simulate(registry, "rank(ts_delta(close,120))", "a120")
        rows = registry.find_similar("rank(ts_delta(close,20))", SETTINGS, top_k=2)
        assert rows[0].expression == "rank(ts_delta(close,21))"
        assert rows[0].similarity > rows[1].similarity
        assert rows[0].similarity > 0.8


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
class TestGates:
    def test_exact_duplicate_is_hard_rejected(self, registry):
        simulate(registry, "rank(ts_delta(close,5))", "a1")
        decision = pre_simulation_gate(registry, "rank(ts_delta(close,5))", SETTINGS)
        assert decision.passed is False
        assert decision.action == "REJECT_EXACT_DUPLICATE"
        assert decision.duplicate.exact_duplicate is True
        assert decision.duplicate.exact_factor_id is not None

    def test_template_detection_across_windows(self, registry):
        for n in (5, 10, 20):
            simulate(registry, f"rank(ts_delta(close,{n}))", f"a{n}")
        dup = duplicate_check(registry, "rank(ts_delta(close,15))", SETTINGS)
        assert dup.template_duplicate is True
        assert dup.template_trials == 3
        assert dup.template == "rank(ts_delta(close,<WINDOW>))"

    def test_unsaturated_template_still_simulates(self, registry):
        for n in (5, 10, 20):
            simulate(registry, f"rank(ts_delta(close,{n}))", f"a{n}")
        decision = pre_simulation_gate(registry, "rank(ts_delta(close,15))", SETTINGS)
        assert decision.passed is True
        assert decision.action == "SIMULATE"

    def test_saturated_template_with_low_novelty_is_skipped(self):
        config = RegistryConfig(
            pre_simulation=PreSimulationConfig(
                max_template_trials=2, min_novelty=50.0,
                reject_if_saturated_and_low_novelty=True,
            )
        )
        import tempfile
        with FactorRegistry(tempfile.mkdtemp() + "/r.db", config) as reg:
            simulate(reg, "rank(ts_delta(close,5))", "a5")
            simulate(reg, "rank(ts_delta(close,10))", "a10")
            decision = pre_simulation_gate(reg, "rank(ts_delta(close,7))", SETTINGS)
            assert decision.passed is False
            assert decision.action == "SKIP_LOW_NOVELTY"
            assert decision.novelty.score < 50.0

    def test_novelty_is_bounded_and_explained(self, registry):
        for n in (5, 10, 20, 60):
            simulate(registry, f"rank(ts_delta(close,{n}))", f"a{n}")
        novelty = calculate_novelty(registry, "rank(ts_delta(close,15))", SETTINGS)
        assert 0.0 <= novelty.score <= 100.0
        assert novelty.explanation
        assert novelty.nearest_factor_id is not None
        # A genuinely fresh family/structure must score much higher.
        fresh = calculate_novelty(
            registry,
            "group_rank(ts_regression(anl4_fs_detail_estimates_advanced_af_nd_fcf_mean, cap, 250, lag=5), market)",
            SETTINGS,
        )
        assert fresh.score > novelty.score

    def test_novelty_for_empty_registry_is_high(self, registry):
        novelty = calculate_novelty(registry, "rank(close)", SETTINGS)
        assert novelty.score >= 90.0


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class TestScoring:
    def test_quality_score_saturates_at_100(self):
        # Turnover inside the healthy 1%..50% band and zero drawdown both
        # contribute their full 5%.
        assert quality_score(2.5, 2.5, turnover=0.1, drawdown=0.0) == 100.0

    def test_quality_score_unknown_metrics(self):
        assert quality_score(None, None) is None  # no performance signal at all
        # Unknown turnover/drawdown degrade to a neutral 0.5 contribution:
        # 0.45*0.4 + 0.45*0.4 + 0.05*0.5 + 0.05*0.5 = 41.0
        assert quality_score(1.0, 1.0) == pytest.approx(41.0)
        # One known secondary metric replaces its neutral fill.
        value = quality_score(1.0, 1.0, turnover=0.3)
        assert value is not None and 0.0 <= value <= 100.0

    def test_research_priority_weights(self):
        assert research_priority(50.0, 100.0) == pytest.approx(0.6 * 50 + 0.4 * 100)

    def test_quality_score_floor_and_bounds(self):
        assert quality_score(-5.0, -5.0, turnover=2.0, drawdown=-3.0) == 0.0


# --------------------------------------------------------------------------- #
# Correlation gate
# --------------------------------------------------------------------------- #
class TestCorrelationGate:
    def test_no_records_is_unknown_not_a_vacuous_pass(self):
        decision = CorrelationGate().evaluate(1, [])
        # v2 contract: no evidence is UNKNOWN, never a pass; submission is
        # blocked by default (allow_submit_without_corr=False).
        assert decision.passed is False
        assert decision.status.value == "UNKNOWN"
        assert decision.band.value == "UNKNOWN"
        assert decision.evaluated == 0
        assert decision.submission_allowed is False
        assert decision.corr_margin is None

    def test_unknown_can_be_overridden_only_via_config(self):
        gate = CorrelationGate(CorrelationConfig(allow_submit_without_corr=True))
        decision = gate.evaluate(1, [])
        assert decision.status.value == "UNKNOWN"
        assert decision.passed is False  # still not a pass...
        assert decision.submission_allowed is True  # ...but manual override allowed

    def test_pending_records_are_unknown(self):
        decision = CorrelationGate().evaluate(
            1,
            [
                {"correlation": None, "status": "PENDING", "other_brain_alpha_id": "x"},
                {"correlation": 0.9, "status": "IN_PROGRESS", "other_brain_alpha_id": "y"},
            ],
        )
        assert decision.status.value == "UNKNOWN"
        assert decision.passed is False
        assert decision.evaluated == 0

    def test_completed_low_corr_is_a_real_pass(self):
        decision = CorrelationGate().evaluate(
            1, [{"correlation": 0.31, "status": "COMPLETED", "other_brain_alpha_id": "a2"}]
        )
        assert decision.status.value == "PASS"
        assert decision.passed is True
        assert decision.band.value == "DIVERSE"
        assert decision.submission_allowed is True
        assert decision.corr_margin == pytest.approx(0.65 - 0.31)

    def test_warning_band_and_very_close_margin(self):
        warning = CorrelationGate().evaluate(
            1, [{"correlation": 0.60, "other_brain_alpha_id": "a"}]
        )
        assert warning.passed is True
        assert warning.band.value == "WARNING"
        close = CorrelationGate().evaluate(
            1, [{"correlation": 0.63, "other_brain_alpha_id": "a"}]
        )
        assert close.very_close_to_limit is True

    def test_positive_over_threshold_fails(self):
        decision = CorrelationGate().evaluate(
            2, [{"correlation": 0.84, "other_brain_alpha_id": "alpha-9"}]
        )
        assert decision.passed is False
        assert decision.max_abs_corr == pytest.approx(0.84)
        assert "alpha-9" in decision.reason

    def test_negative_correlation_blocked_in_abs_mode(self):
        decision = CorrelationGate(CorrelationConfig(use_absolute_value=True)).evaluate(
            3, [{"correlation": -0.9, "other_brain_alpha_id": "alpha-1"}]
        )
        assert decision.passed is False
        assert decision.max_corr == pytest.approx(-0.9)

    def test_negative_correlation_passes_in_signed_mode(self):
        decision = CorrelationGate(CorrelationConfig(use_absolute_value=False)).evaluate(
            3, [{"correlation": -0.9, "other_brain_alpha_id": "alpha-1"}]
        )
        assert decision.passed is True

    def test_within_threshold_passes(self):
        decision = CorrelationGate().evaluate(
            4, [{"correlation": 0.31, "other_brain_alpha_id": "alpha-2"}]
        )
        assert decision.passed is True

    def test_registry_applies_gate_and_persists_status(self, registry):
        fid0 = simulate(registry, "rank(ts_delta(close,22))", "live1", sharpe=1.6)
        registry.mark_submitted(fid0, brain_alpha_id="live1")
        fid1 = simulate(registry, "rank(ts_delta(close,23))", "live2", sharpe=1.7)
        registry.save_correlations(
            fid1,
            [{"correlation": 0.84, "other_brain_alpha_id": "live1"}],
            CORR_TYPE_SELF,
        )
        decision = registry.apply_correlation_gate(fid1)
        assert decision.passed is False
        assert registry.get_factor(fid1)["status"] == FactorStatus.CORR_REJECTED

    def test_correlation_upsert_is_idempotent(self, registry):
        fid = simulate(registry, "rank(close)", "a")
        records = [{"correlation": 0.84, "other_brain_alpha_id": "live1"}]
        assert registry.save_correlations(fid, records, CORR_TYPE_SELF) == 1
        assert registry.save_correlations(fid, records, CORR_TYPE_SELF) == 1
        rows = registry._query("SELECT COUNT(*) AS n FROM factor_correlations")
        assert rows[0]["n"] == 1


# --------------------------------------------------------------------------- #
# Clusters
# --------------------------------------------------------------------------- #
class TestClusters:
    def test_connected_components_and_representative(self, registry):
        fid0 = simulate(registry, "rank(ts_delta(close,22))", "live1", sharpe=1.4)
        registry.mark_submitted(fid0, brain_alpha_id="live1")
        fid1 = simulate(registry, "rank(ts_delta(close,23))", "live2", sharpe=2.2)
        fid2 = simulate(registry, "rank(ts_delta(close,24))", "live3", sharpe=1.0)
        # 0-1 and 1-2 edges above 0.70 -> one component {0,1,2}.
        registry.save_correlations(
            fid1, [{"correlation": 0.81, "other_brain_alpha_id": "live1"}], CORR_TYPE_SELF
        )
        registry.save_correlations(
            fid2, [{"correlation": 0.75, "other_brain_alpha_id": "live2"}], CORR_TYPE_SELF
        )
        clusters = get_clusters(registry)
        multi = [c for c in clusters if c.size > 1]
        assert len(multi) == 1
        assert set(multi[0].factor_ids) == {fid0, fid1, fid2}
        # SUBMITTED alpha wins representative priority despite lower sharpe.
        assert multi[0].representative_id == fid0
        assert multi[0].submitted_count == 1

    def test_singletons_are_their_own_clusters(self, registry):
        simulate(registry, "rank(close)", "a")
        simulate(registry, "rank(open)", "b")
        reps = get_cluster_representatives(registry)
        assert {c["representative_id"] for c in reps} == {1, 2}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
class TestPersistence:
    def test_close_and_reopen_keeps_state(self, tmp_path):
        db = tmp_path / "r.db"
        with FactorRegistry(db, RegistryConfig()) as reg:
            simulate(reg, "rank(ts_delta(close,5))", "a1")
        with FactorRegistry(db, RegistryConfig()) as reg:
            row = reg.find_exact("rank(ts_delta(close,5))", SETTINGS)
            assert row is not None
            assert row["status"] == FactorStatus.SIMULATED
            assert reg.stats()["total"] == 1


# --------------------------------------------------------------------------- #
# Historical migration
# --------------------------------------------------------------------------- #
def seed_old_worldquant_db(path, rows: list[dict]):
    """Create a throwaway worldquant.db populated with given result specs."""
    with ResultStore(path) as store:
        for index, row in enumerate(rows):
            expression = row["expression"]
            settings = row.get("settings", SETTINGS)
            name = f"alpha_{index}"
            alpha_row = store.upsert_alpha(expression, settings, name=name)
            sim_row = store.create_simulation(
                alpha_row,
                remote_simulation_id=f"rsim_{index}",
                status=SimulationStatus.SUBMITTED,
            )
            status = row.get("status", SimulationStatus.COMPLETED)
            store.update_simulation(
                sim_row,
                status=status,
                remote_alpha_id=row.get("remote_id", f"ralpha_{index}"),
                mark_completed=(status == SimulationStatus.COMPLETED),
            )
            result = AlphaResult(
                alpha_id=name,
                expression=expression,
                dedup_key=__import__("worldquant.hashing", fromlist=["dedup_key"]).dedup_key(
                    expression, settings
                ),
                status=status,
                simulation_id=f"rsim_{index}",
                remote_alpha_id=row.get("remote_id", f"ralpha_{index}"),
                settings_json=json.dumps(settings, sort_keys=True),
                sharpe=row.get("sharpe", 1.5),
                fitness=row.get("fitness", 1.2),
                turnover=0.3,
                returns=0.1,
                drawdown=-0.08,
                margin=0.001,
                long_count=100,
                short_count=100,
                grade=row.get("grade", "GOOD"),
                passed=row.get("passed", True if status == SimulationStatus.COMPLETED else None),
                reasons=row.get("reasons", []),
                self_correlation=row.get("self_correlation"),
                submission_checks=row.get("submission_checks"),
                created_at=utcnow_iso(),
                completed_at=utcnow_iso() if status == SimulationStatus.COMPLETED else None,
                error=row.get("error"),
            )
            store.save_result(sim_row, result)


class TestMigration:
    def test_status_mapping(self, tmp_path):
        old_db = tmp_path / "worldquant.db"
        seed_old_worldquant_db(old_db, [
            {"expression": "rank(ts_delta(close,5))", "remote_id": "r1", "sharpe": 1.6},
            {"expression": "rank(ts_delta(open,5))", "remote_id": "r2", "sharpe": 0.5,
             "passed": False, "reasons": ["LOW_SHARPE"]},
            {"expression": "rank(ts_delta(vwap,5))", "remote_id": "r3",
             "passed": None, "self_correlation": 0.84},
            {"expression": "rank(ts_delta(volume,5))", "remote_id": "r4", "sharpe": 2.0,
             "submission_checks": submission_checks()},
            {"expression": "rank(broken)", "remote_id": "r5",
             "status": SimulationStatus.FAILED, "passed": None, "error": "boom"},
        ])
        with FactorRegistry(tmp_path / "reg.db", RegistryConfig()) as reg:
            summary = migrate_existing_results(old_db, reg)
            assert summary["rows_seen"] == 5
            assert summary["failed"] == 1
            counts = reg.status_counts()
            assert counts[FactorStatus.SIMULATED] == 1
            assert counts[FactorStatus.METRIC_REJECTED] == 1
            assert counts[FactorStatus.CORR_REJECTED] == 1
            assert counts[FactorStatus.PASSED] == 1
            assert counts[FactorStatus.SIMULATION_FAILED] == 1
            # The SELF_MAX value from the old DB row is stored verbatim.
            corr_rows = reg._query("SELECT correlation FROM factor_correlations")
            assert any(abs(row["correlation"] - 0.84) < 1e-9 for row in corr_rows)

    def test_migration_is_idempotent(self, tmp_path):
        old_db = tmp_path / "worldquant.db"
        seed_old_worldquant_db(old_db, [
            {"expression": "rank(ts_delta(close,5))", "remote_id": "r1"},
            {"expression": "rank(ts_delta(close,10))", "remote_id": "r2",
             "passed": False, "reasons": ["LOW_SHARPE"]},
        ])
        with FactorRegistry(tmp_path / "reg.db", RegistryConfig()) as reg:
            first = migrate_existing_results(old_db, reg)
            total_after_first = reg.stats()["total"]
            second = migrate_existing_results(old_db, reg)
            assert reg.stats()["total"] == total_after_first == 2
            assert first["rows_seen"] == second["rows_seen"] == 2
            assert second["already_present"] == 2


# --------------------------------------------------------------------------- #
# Live account import
# --------------------------------------------------------------------------- #
class FakeAccountClient:
    """Duck-typed client for import_submitted_factors (no network)."""

    def __init__(self, items):
        self._items = items
        self.auth_calls = 0

    def ensure_authenticated(self):
        self.auth_calls += 1

    def list_self_alphas(self, *, max_pages=None):
        return list(self._items)


def _account_item(alpha_id: str, expression: str, *, sharpe: float = 1.6) -> dict:
    return {
        "alpha_id": alpha_id,
        "expression": expression,
        "settings": dict(SETTINGS),
        "grade": "GOOD",
        "stage": None,
        "status": "UNSUBMITTED",
        "metrics": {"sharpe": sharpe, "fitness": 1.3, "turnover": 0.3, "returns": 0.1,
                    "drawdown": -0.08, "margin": 0.001, "pnl": 1.0, "book_size": 1e7,
                    "long_count": 100, "short_count": 100},
        "train": None,
        "test": None,
        "checks": {},
        "raw": {},
    }


class TestImportSubmitted:
    def test_import_then_idempotent_rerun(self, registry):
        client = FakeAccountClient([
            _account_item("brain-1", "rank(ts_delta(close,5))"),
            _account_item("brain-2", "rank(ts_delta(open,10))", sharpe=2.1),
        ])
        first = import_submitted_factors(client, registry)
        assert first["imported"] == 2
        assert client.auth_calls == 1
        counts = registry.status_counts()
        assert counts[FactorStatus.SUBMITTED] == 2
        factor = registry._query(
            "SELECT brain_alpha_id, sharpe FROM factors f JOIN factor_metrics m "
            "ON m.factor_id = f.id WHERE brain_alpha_id = ?",
            ("brain-2",),
        )
        assert factor and abs(factor[0]["sharpe"] - 2.1) < 1e-9

        second = import_submitted_factors(client, registry)
        assert second["imported"] == 0
        assert second["updated"] == 2
        assert registry.stats()["total"] == 2


# --------------------------------------------------------------------------- #
# Client pagination + API parsing
# --------------------------------------------------------------------------- #
BASE = "https://api.worldquantbrain.com"


class TestSelfAlphasClient:
    def test_pagination_follows_next_link(self):
        page1 = {
            "count": 2,
            "next": f"{BASE}/users/self/alphas?limit=1&offset=1",
            "results": [{
                "id": "a1", "regular": "rank(close)",
                "settings": {"region": "USA"},
                "is": {"sharpe": 1.5, "fitness": 1.2, "turnover": 0.3},
            }],
        }
        page2 = {
            "count": 2, "next": None,
            "results": [{
                "id": "a2", "regular": "-rank(open)",
                "settings": {},
                "is": {"sharpe": 2.0},
            }],
        }

        def handler(method, url, kwargs):
            if url == f"{BASE}/users/self/alphas":
                return FakeResponse(200, page1)
            if "offset=1" in url:
                return FakeResponse(200, page2)
            raise AssertionError(f"unexpected url {url}")

        from worldquant.client import WorldQuantClient
        from worldquant.config import Credentials, RetryConfig
        session = FakeSession(handler)
        client = WorldQuantClient(
            Credentials("t@example.com", "s"),
            base_url=BASE, retry=RetryConfig(max_retries=1),
            min_request_interval=0.0, session=session,
        )
        client._authenticated = True
        items = client.list_self_alphas(limit=1)
        assert [item["alpha_id"] for item in items] == ["a1", "a2"]
        assert len(session.calls) == 2
        # The second call uses the server-provided next URL verbatim.
        assert session.calls[1][1].endswith("/users/self/alphas?limit=1&offset=1")
        assert session.calls[1][2].get("params") is None

    def test_max_pages_caps_paging(self):
        page = {"count": 1, "next": f"{BASE}/users/self/alphas?offset=1",
                "results": [{"id": "a1", "regular": "rank(close)", "settings": {}}]}

        def handler(method, url, kwargs):
            return FakeResponse(200, page)

        from worldquant.client import WorldQuantClient
        from worldquant.config import Credentials, RetryConfig
        session = FakeSession(handler)
        client = WorldQuantClient(
            Credentials("t@example.com", "s"),
            base_url=BASE, retry=RetryConfig(max_retries=1),
            min_request_interval=0.0, session=session,
        )
        client._authenticated = True
        items = client.list_self_alphas(max_pages=1)
        assert len(items) == 1


class TestSelfAlphasParsing:
    def test_skips_items_without_expression(self):
        from worldquant.api import parse_self_alphas_page
        parsed = parse_self_alphas_page({
            "count": 2,
            "next": None,
            "results": [
                {"id": "a1", "regular": "rank(close)", "settings": {}, "is": {}},
                {"id": "a2", "settings": {}, "is": {}},
            ],
        })
        assert [r["alpha_id"] for r in parsed["results"]] == ["a1"]
        assert parsed["results"][0]["metrics"]["sharpe"] is None

    def test_extract_expression_variants(self):
        from worldquant.api import extract_alpha_expression
        assert extract_alpha_expression({"regular": "rank(close)"}) == "rank(close)"
        assert extract_alpha_expression({"expression": "rank(open)"}) == "rank(open)"
        assert extract_alpha_expression({"regular": {"code": "rank(high)"}}) == "rank(high)"
        assert extract_alpha_expression({"id": "x"}) == ""


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class TestAdapter:
    def test_open_registry_none_when_disabled(self, tmp_path):
        config = make_config(tmp_path)  # registry defaults to enabled=False
        assert open_registry(config) is None

    def test_open_registry_enabled(self, tmp_path):
        config = replace(
            make_config(tmp_path),
            registry=replace(
                make_config(tmp_path).registry,
                enabled=True, db_path=tmp_path / "fr.db",
            ),
        )
        registry = open_registry(config)
        assert registry is not None
        registry.close()

    def test_gate_candidate_blocks_exact_duplicate_without_erasing_memory(self, tmp_path):
        config = replace(
            make_config(tmp_path),
            registry=RegistryConfig(enabled=True, db_path=tmp_path / "fr.db"),
        )
        with open_registry(config) as registry:
            prior_id = simulate(registry, "rank(ts_delta(close,5))", "a1")
            prior_status = registry.get_factor(prior_id)["status"]
            factor_id, decision = gate_candidate(
                registry, "rank(ts_delta(close,5))", SETTINGS
            )
            assert decision.action == "REJECT_EXACT_DUPLICATE"
            # The blocked attempt points at the researched prior row, whose
            # lifecycle status is research memory and must not be erased.
            assert factor_id == prior_id
            assert registry.get_factor(factor_id)["status"] == prior_status
            assert registry.get_factor(factor_id)["status"] != FactorStatus.DUPLICATE

    def test_gate_candidate_passes_fresh_candidate_and_recovers_generated(self, tmp_path):
        config = replace(
            make_config(tmp_path),
            registry=RegistryConfig(enabled=True, db_path=tmp_path / "fr.db"),
        )
        with open_registry(config) as registry:
            # A never-seen experiment must pass the gate.
            factor_id, decision = gate_candidate(
                registry, "rank(ts_delta(close,5))", SETTINGS
            )
            assert decision.passed is True
            assert decision.action == "SIMULATE"
            assert registry.get_factor(factor_id)["status"] == FactorStatus.GENERATED
            # An interrupted prior run left a GENERATED row: the retry must be
            # allowed to simulate instead of self-blocking as an exact dup.
            retried_id, retried = gate_candidate(
                registry, "rank(ts_delta(close,5))", SETTINGS
            )
            assert retried_id == factor_id
            assert retried.passed is True
            assert retried.action == "SIMULATE"

    def test_record_completed_folds_result(self, registry):
        registry.register_candidate("rank(close)", SETTINGS)
        outcome = record_completed(registry, make_completed("rank(close)", "a1"))
        assert outcome["status"] == FactorStatus.SIMULATED


# --------------------------------------------------------------------------- #
# Generation context
# --------------------------------------------------------------------------- #
class TestGenerationContext:
    def test_context_sections_and_diversity(self, registry):
        fid = simulate(registry, "ts_rank(returns,5)+rank(volume)", "a1", sharpe=1.8)
        registry.mark_submitted(fid, brain_alpha_id="a1")
        simulate(registry, "rank(ts_delta(close,5))", "a2", sharpe=0.4, passed=False,
                 reasons=["LOW_SHARPE"])
        context = build_generation_context(registry)
        for key in (
            "saturated_families", "underexplored_families", "overused_structures",
            "most_common_fields", "high_corr_patterns", "saturated_combinations",
            "underexplored_combinations", "recent_rejected_examples",
            "successful_diverse_examples", "summary",
        ):
            assert key in context
        assert context["summary"]["submitted"] == 1
        assert any(
            example["brain_alpha_id"] == "a1"
            for example in context["successful_diverse_examples"]
        )
        fields = {item["field"] for item in context["most_common_fields"]}
        assert {"returns", "volume", "close"} <= fields


# --------------------------------------------------------------------------- #
# Config wiring
# --------------------------------------------------------------------------- #
class TestRegistryConfig:
    def test_unknown_key_raises(self):
        with pytest.raises(ConfigError):
            build_registry_config({"enabled": True, "bogus": 1})

    def test_unknown_nested_key_raises(self):
        with pytest.raises(ConfigError):
            build_registry_config({"correlation": {"threshold": 0.7, "nope": True}})

    def test_threshold_validation(self):
        with pytest.raises(ConfigError):
            build_registry_config({"correlation": {"threshold": 1.5}})

    def test_shipped_config_yaml_enables_registry(self):
        from pathlib import Path
        project_root = Path(__file__).resolve().parent.parent
        config = load_config(project_root / "config.yaml")
        assert config.registry.enabled is True
        assert config.registry.correlation.threshold == pytest.approx(0.70)
        assert config.registry.novelty.normalized()["structure"] == pytest.approx(0.30)
        assert config.registry.db_path.name == "factor_registry.db"
