"""v5 correlation-evidence finality classification.

SELF_CORRELATION is only scheduled by the platform once every other
submission check passes. An alpha failing another gate keeps SELF_CORRELATION
PENDING forever, and an ALREADY_SUBMITTED duplicate never exposes a recordset.
Both must leave the backfill target set (with corr verdict still UNKNOWN —
never fabricated), while a genuinely-pending alpha that passes every other
gate must stay a target.
"""

from __future__ import annotations

from worldquant.registry import FactorRegistry
from worldquant.registry.config import RegistryConfig
from worldquant.registry.refresh import backfill_correlations
from worldquant.registry.store import CORR_TYPE_SELF

SETTINGS = {"region": "USA", "universe": "TOP3000", "delay": 1}


class FakeClient:
    """Returns canned parsed /check payloads keyed by brain alpha id."""

    def __init__(self, payloads: dict[str, dict]):
        self._payloads = payloads
        self.calls: list[str] = []

    def check_submission(self, alpha_id: str) -> dict:
        self.calls.append(alpha_id)
        return self._payloads[alpha_id]


def _check(*, failing=(), self_result="PENDING", neighbors=None,
           already_submitted=False) -> dict:
    names = [
        "LOW_SHARPE", "LOW_FITNESS", "LOW_TURNOVER", "HIGH_TURNOVER",
        "CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE",
        "SELF_CORRELATION", "MATCHES_COMPETITION",
    ]
    checks = {}
    for name in names:
        if name == "SELF_CORRELATION":
            checks[name] = {"result": self_result}
        else:
            checks[name] = {"result": "FAIL" if name in failing else "PASS"}
    if already_submitted:
        return {
            "checks": {"ALREADY_SUBMITTED": {"result": "FAIL"}},
            "self_correlation": None,
            "self_correlated_with": [],
            "passed_count": 0, "total": 1, "all_passed": False,
            "failures": ["ALREADY_SUBMITTED"],
        }
    return {
        "checks": checks,
        "self_correlation": None,
        "self_correlated_with": neighbors or [],
        "passed_count": sum(
            1 for c in checks.values() if c["result"] == "PASS"),
        "total": len(checks),
        "all_passed": not failing and self_result == "PASS",
        "failures": list(failing) + (["SELF_CORRELATION"]
                                     if self_result != "PASS" else []),
    }


def _remaining(registry: FactorRegistry) -> set[str]:
    return {
        f["brain_alpha_id"]
        for f in registry.list_corr_backfill_targets(limit=100)
    }


def _note(registry: FactorRegistry, factor_id: int) -> str | None:
    return registry.get_factor(factor_id)["corr_evidence_note"]


def test_gated_alpha_is_finalized_after_attempt_guard(tmp_path):
    with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
        fid = registry.register_candidate(
            "rank(close)", SETTINGS, brain_alpha_id="G1"
        ).factor_id
        client = FakeClient({"G1": _check(failing=["LOW_FITNESS"])})

        # First two observations: still inside the guard window -> stays.
        backfill_correlations(client, registry, limit=10)
        backfill_correlations(client, registry, limit=10)
        assert "G1" in _remaining(registry)

        # Third observation (>= 2 prior attempts): terminal GATED note.
        backfill_correlations(client, registry, limit=10)
        assert "G1" not in _remaining(registry)
        note = _note(registry, fid)
        assert note is not None and note.startswith("GATED:")
        assert "LOW_FITNESS" in note
        # Verdict must never be fabricated from a missing corr computation.
        assert registry.get_factor(fid)["corr_status"] is None


def test_pending_with_all_other_gates_passing_stays_a_target(tmp_path):
    with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
        registry.register_candidate(
            "rank(close)", SETTINGS, brain_alpha_id="P1"
        )
        client = FakeClient({"P1": _check(self_result="PENDING")})
        for _ in range(6):
            backfill_correlations(client, registry, limit=10)
        assert "P1" in _remaining(registry)


def test_already_submitted_duplicate_is_final_with_note(tmp_path):
    with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
        fid = registry.register_candidate(
            "rank(close)", SETTINGS, brain_alpha_id="D1"
        ).factor_id
        client = FakeClient({"D1": _check(already_submitted=True)})
        backfill_correlations(client, registry, limit=10)
        assert "D1" not in _remaining(registry)
        assert _note(registry, fid) == "ALREADY_SUBMITTED"


def test_zero_neighbour_self_pass_gets_self_pass_note(tmp_path):
    with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
        fid = registry.register_candidate(
            "rank(close)", SETTINGS, brain_alpha_id="Z1"
        ).factor_id
        client = FakeClient({"Z1": _check(self_result="PASS")})
        backfill_correlations(client, registry, limit=10)
        assert "Z1" not in _remaining(registry)
        assert _note(registry, fid) == "SELF_PASS"


def test_neighbor_recordset_gets_self_neighbors_note_and_edges(tmp_path):
    with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
        fid = registry.register_candidate(
            "rank(close)", SETTINGS, brain_alpha_id="N1"
        ).factor_id
        other = registry.register_candidate(
            "-rank(close)", SETTINGS, brain_alpha_id="N2"
        ).factor_id
        payload = _check(
            self_result="FAIL",
            neighbors=[{"other_factor_id": other, "correlation": 0.91}],
        )
        client = FakeClient({"N1": payload})
        summary = backfill_correlations(client, registry, limit=10)
        assert summary["neighbors_saved"] >= 1
        assert "N1" not in _remaining(registry)
        assert _note(registry, fid) == "SELF_NEIGHBORS"
        edges = registry._query(
            "SELECT correlation_type FROM factor_correlations WHERE factor_id=?",
            (fid,),
        )
        assert {row["correlation_type"] for row in edges} == {CORR_TYPE_SELF}


def test_gated_run_is_idempotent(tmp_path):
    with FactorRegistry(tmp_path / "r.db", RegistryConfig()) as registry:
        registry.register_candidate(
            "rank(close)", SETTINGS, brain_alpha_id="G1"
        )
        client = FakeClient({"G1": _check(failing=["LOW_SHARPE"])})
        for _ in range(3):
            backfill_correlations(client, registry, limit=10)
        calls_after_gate = len(client.calls)
        # Nothing left to do: the gated alpha must never be fetched again.
        backfill_correlations(client, registry, limit=10)
        assert len(client.calls) == calls_after_gate
