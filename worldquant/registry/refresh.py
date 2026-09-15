"""Offline correlation evidence refresh.

UNKNOWN correlation verdicts are "evidence not available yet", never a final
rejection, so they must be resolvable *without* spending simulation quota or
submitting anything. These operations reuse the already-proven
``GET /alphas/{id}/check`` endpoint (read-only polling, exactly as
:class:`~worldquant.simulator.SimulationRunner` does after a simulation) to:

* :func:`refresh_unknown_correlations` — re-check SIMULATED factors whose
  corr verdict is UNKNOWN, promote them to PASSED / CORR_REJECTED when evidence
  arrives, and record attempt/error bookkeeping so a permanently failing id is
  visible instead of silently retried forever.
* :func:`backfill_correlations` — pull per-neighbor SELF edges for factors that
  only have the SELF_MAX aggregate (older runs), which makes the correlation
  graph and clustering real instead of approximate.

No simulation, no submission, no quota consumption.
"""

from __future__ import annotations

from typing import Any

from ..logging_utils import get_logger
from .correlation import CorrelationStatus
from .store import CORR_TYPE_SELF, CORR_TYPE_SELF_MAX, FactorRegistry, FactorStatus

#: Minimum number of recorded /check attempts before a PENDING
#: SELF_CORRELATION combined with another FAIL gate is accepted as terminal.
#: Protects a freshly simulated alpha whose metric checks have not themselves
#: resolved on the very first poll.
_GATE_MIN_ATTEMPTS = 2


def _refresh_one(
    client: Any,
    registry: FactorRegistry,
    factor: dict[str, Any],
    *,
    log: Any,
) -> tuple[str, int]:
    """Refresh one factor. Returns (verdict, neighbors_written).

    verdict is one of ``PASS`` / ``FAIL`` / ``UNKNOWN`` / ``ERROR``.
    """
    factor_id = int(factor["id"])
    alpha_id = factor.get("brain_alpha_id")
    if not alpha_id:
        return "ERROR", 0
    try:
        checked = client.check_submission(str(alpha_id))
    except Exception as exc:  # noqa: BLE001 - network boundary, keep looping
        log.warning("correlation refresh failed for %s: %s", alpha_id, exc)
        registry.mark_corr_checked(factor_id, error=str(exc))
        return "ERROR", 0

    neighbors = checked.get("self_correlated_with") if isinstance(checked, dict) else None
    max_self = checked.get("self_correlation") if isinstance(checked, dict) else None
    checks = checked.get("checks") if isinstance(checked, dict) else None
    total_checks = checked.get("total") if isinstance(checked, dict) else None
    neighbors_written = 0

    # check_submission degrades a throttled / not-yet-computed answer to the
    # empty shape instead of raising. That is *not* evidence and must remain a
    # retry target; record it as an error so callers can tell throttling apart
    # from a parsed PENDING (the normal state right after first triggering).
    if not total_checks and not neighbors and max_self is None:
        registry.mark_corr_checked(
            factor_id, error="check unavailable (throttled or still computing)"
        )
        return "ERROR", 0

    if isinstance(neighbors, list) and neighbors:
        neighbors_written = registry.save_correlations(
            factor_id, neighbors, CORR_TYPE_SELF
        )
    elif max_self is not None:
        # Aggregate only: keep feeding the gate, but the graph stays edgeless.
        registry.save_correlations(
            factor_id, [{"correlation": float(max_self)}], CORR_TYPE_SELF_MAX
        )

    # Decide whether the /check delivered *final* self-correlation evidence.
    # A concrete neighbour recordset is always final (edges just written); a
    # resolved SELF_CORRELATION check is final too — including a zero-neighbour
    # PASS, which is authoritative "no pairwise edges".
    self_result = ""
    checks_by_name = checks if isinstance(checks, dict) else {}
    self_check = checks_by_name.get("SELF_CORRELATION")
    if isinstance(self_check, dict):
        self_result = str(self_check.get("result") or "").upper()

    # The platform only schedules the SELF_CORRELATION computation once every
    # other submission gate passes; an alpha failing e.g. LOW_FITNESS keeps
    # SELF_CORRELATION at PENDING for its entire lifetime (empirically
    # re-polling for >12h never changes it). Likewise an ALREADY_SUBMITTED
    # duplicate's /check only echoes that one verdict and never exposes a
    # corr recordset. Both are terminal *evidence-not-available* states: the
    # corr verdict stays UNKNOWN (never fabricated), but the factor must leave
    # the backfill set instead of being re-fetched forever. The small attempt
    # guard avoids finalizing a brand-new alpha whose other checks have not
    # themselves resolved yet.
    note: str | None = None
    if isinstance(neighbors, list) and neighbors:
        note = "SELF_NEIGHBORS"
    elif self_result == "PASS":
        note = "SELF_PASS"
    elif self_result == "FAIL":
        note = "SELF_FAIL"
    elif self_result == "ERROR":
        note = "SELF_ERROR"
    else:
        already_submitted = (
            isinstance(checks_by_name.get("ALREADY_SUBMITTED"), dict)
            and str(
                checks_by_name["ALREADY_SUBMITTED"].get("result") or ""
            ).upper()
            == "FAIL"
        )
        other_failures = sorted(
            name
            for name, check in checks_by_name.items()
            if name not in {"SELF_CORRELATION", "ALREADY_SUBMITTED"}
            and isinstance(check, dict)
            and str(check.get("result") or "").upper() == "FAIL"
        )
        attempts = int(factor.get("corr_check_attempts") or 0)
        if already_submitted:
            note = "ALREADY_SUBMITTED"
        elif other_failures and attempts >= _GATE_MIN_ATTEMPTS:
            note = "GATED:" + ",".join(other_failures)

    verdict = CorrelationStatus.UNKNOWN.value
    if factor.get("status") == FactorStatus.SIMULATED and note in {
        "SELF_NEIGHBORS", "SELF_PASS", "SELF_FAIL", "SELF_ERROR",
    }:
        decision = registry.apply_correlation_gate(factor_id)
        verdict = decision.status.value

    if note is not None:
        registry.mark_corr_evidence_final(factor_id, note=note)
    else:
        # PENDING / missing with no gating failure: the async check has not
        # computed yet and the factor stays a target.
        registry.mark_corr_checked(factor_id)
    return verdict, neighbors_written


def refresh_unknown_correlations(
    client: Any,
    registry: FactorRegistry,
    *,
    limit: int = 50,
    alpha_id: str | None = None,
    log: Any = None,
) -> dict[str, int]:
    """Re-check SIMULATED factors with an UNKNOWN corr verdict.

    Ordered by fewest previous attempts (see
    :meth:`FactorRegistry.list_unresolved_corr_factors`).
    """
    log = log or get_logger("registry.refresh")
    factors = registry.list_unresolved_corr_factors(limit=limit, alpha_id=alpha_id)
    summary = {
        "candidates": len(factors), "passed": 0, "rejected": 0,
        "unknown": 0, "errors": 0, "neighbors_saved": 0,
    }
    log.info("refreshing correlation for %d UNKNOWN factor(s)", len(factors))
    for factor in factors:
        verdict, neighbors_written = _refresh_one(
            client, registry, factor, log=log
        )
        summary["neighbors_saved"] += neighbors_written
        if verdict == CorrelationStatus.PASS.value:
            summary["passed"] += 1
        elif verdict == CorrelationStatus.FAIL.value:
            summary["rejected"] += 1
        elif verdict == CorrelationStatus.UNKNOWN.value:
            summary["unknown"] += 1
        else:
            summary["errors"] += 1

    reconciliation = registry.reconcile_correlation_neighbors()
    summary["neighbors_resolved"] = reconciliation["resolved"]
    return summary


def backfill_correlations(
    client: Any,
    registry: FactorRegistry,
    *,
    limit: int = 100,
    log: Any = None,
) -> dict[str, int]:
    """Pull per-neighbor SELF edges for factors that only have SELF_MAX.

    Lifecycle status is never changed here except for still-SIMULATED factors,
    where the fresh evidence is immediately run through the research gate.
    """
    log = log or get_logger("registry.refresh")
    factors = registry.list_corr_backfill_targets(limit=limit)
    summary = {
        "candidates": len(factors), "passed": 0, "rejected": 0,
        "unknown": 0, "errors": 0, "neighbors_saved": 0,
    }
    log.info("backfilling per-neighbor correlations for %d factor(s)", len(factors))
    for factor in factors:
        verdict, neighbors_written = _refresh_one(
            client, registry, factor, log=log
        )
        summary["neighbors_saved"] += neighbors_written
        if verdict == CorrelationStatus.PASS.value:
            summary["passed"] += 1
        elif verdict == CorrelationStatus.FAIL.value:
            summary["rejected"] += 1
        elif verdict == CorrelationStatus.UNKNOWN.value:
            summary["unknown"] += 1
        else:
            summary["errors"] += 1

    reconciliation = registry.reconcile_correlation_neighbors()
    summary["neighbors_resolved"] = reconciliation["resolved"]
    return summary
