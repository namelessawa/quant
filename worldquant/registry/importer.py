"""Seed the registry without re-running a single historical alpha.

Two seed sources:

* :func:`migrate_existing_results` reads the project's proven
  ``data/worldquant.db`` (:class:`~worldquant.storage.ResultStore`) and folds
  every past simulation — completed, failed, metric-rejected and correlation-
  blocked alike — into the registry. Missing metadata stays NULL rather than
  discarding the row.
* :func:`import_submitted_factors` pages ``GET /users/self/alphas`` through the
  existing :class:`~worldquant.client.WorldQuantClient` and marks the account's
  live alphas as SUBMITTED.

Both paths are idempotent: identity is ``exact_hash`` (expression + settings),
falling back to ``brain_alpha_id``; counters only move on first ingestion.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..api import SimulationStatus, normalize_settings
from ..hashing import dedup_key
from ..logging_utils import get_logger
from ..storage import ResultStore
from .store import CORR_TYPE_SELF_MAX, FactorRegistry, FactorStatus


def _already_processed(existing: dict[str, Any] | None) -> bool:
    """A factor whose research lifecycle was already ingested."""
    if not existing:
        return False
    return existing["status"] in (
        FactorStatus.RESEARCH_SET
        | {FactorStatus.SIMULATION_FAILED, FactorStatus.SUBMIT_FAILED}
    )


def migrate_existing_results(
    worldquant_db_path: str | Path,
    registry: FactorRegistry,
    *,
    log: Any = None,
) -> dict[str, int]:
    """Copy every historical simulation from the old DB into the registry.

    Opens the source database read-mostly through the existing
    :class:`ResultStore` (its schema migrations are additive and safe), then
    reuses :meth:`FactorRegistry.save_simulation_result` with duck-typed
    :class:`~worldquant.models.AlphaResult` objects.
    """
    log = log or get_logger("registry.importer")
    source_path = Path(worldquant_db_path)
    if not source_path.exists():
        raise FileNotFoundError(f"historical database not found: {source_path}")

    summary = {
        "rows_seen": 0,
        "processed": 0,
        "superseded_runs": 0,
        "completed": 0,
        "failed": 0,
        "passed": 0,
        "corr_rejected": 0,
        "metric_rejected": 0,
        "simulated": 0,
        "already_present": 0,
        "correlations_saved": 0,
    }

    with ResultStore(source_path) as source:
        all_rows = source.all_results()
        # Keep only the LATEST stored run per expression+settings: an alpha may
        # have been SKIPPED once and then COMPLETED, and the final verdict must
        # win. all_results() returns rows in insertion (chronological) order.
        latest_by_key: dict[str, Any] = {}
        for result in all_rows:
            summary["rows_seen"] += 1
            try:
                settings = json.loads(result.settings_json or "{}")
            except (json.JSONDecodeError, TypeError):
                settings = {}
            key = dedup_key(result.expression, normalize_settings(settings))
            latest_by_key[key] = (result, settings)

        results = [pair[0] for pair in latest_by_key.values()]
        summary["superseded_runs"] = summary["rows_seen"] - len(results)

        for result in results:
            try:
                settings = json.loads(result.settings_json or "{}")
            except (json.JSONDecodeError, TypeError):
                settings = {}
            existing = registry.find_exact(result.expression, settings)
            if _already_processed(existing):
                # Skip BEFORE save: replaying save_simulation_result would
                # overwrite an already-derived lifecycle (PASSED / CORR_REJECTED)
                # with this row's raw passed/self_correlation verdict.
                summary["already_present"] += 1
                if result.status == SimulationStatus.COMPLETED:
                    summary["completed"] += 1
                else:
                    summary["failed"] += 1
                continue

            # Tag the object so the registry records the right provenance.
            try:
                result.registry_source = "historical_import"  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - frozen/odd objects stay untagged
                pass

            factor_id = registry.save_simulation_result(result)
            summary["processed"] += 1

            if result.status != SimulationStatus.COMPLETED:
                summary["failed"] += 1
                continue

            summary["completed"] += 1
            self_corr = result.self_correlation
            if self_corr is not None:
                summary["correlations_saved"] += registry.save_correlations(
                    factor_id,
                    [{"correlation": float(self_corr)}],
                    CORR_TYPE_SELF_MAX,
                )

            excluded = (result.excluded_reason or "").strip()
            if excluded:
                if "SELF_CORR" in excluded.upper():
                    registry.set_status(
                        factor_id, FactorStatus.CORR_REJECTED,
                        rejection_reason=excluded[:500],
                        brain_alpha_id=result.remote_alpha_id,
                    )
                    summary["corr_rejected"] += 1
                else:
                    registry.set_status(
                        factor_id, FactorStatus.METRIC_REJECTED,
                        rejection_reason=excluded[:500],
                        brain_alpha_id=result.remote_alpha_id,
                    )
                    summary["metric_rejected"] += 1
                continue

            if result.is_submittable:
                # Official 8/8 verdict (SELF_CORRELATION included) — the
                # research gate does not override BRAIN's own PASS.
                registry.set_status(
                    factor_id, FactorStatus.PASSED,
                    brain_alpha_id=result.remote_alpha_id,
                )
                summary["passed"] += 1
                continue

            if result.passed is None:
                # Completed but never evaluated by the local metric filters:
                # a neutral SIMULATED row, then let correlation decide.
                registry.set_status(
                    factor_id, FactorStatus.SIMULATED,
                    brain_alpha_id=result.remote_alpha_id,
                )
                summary["simulated"] += 1

            if result.passed is not False and self_corr is not None:
                decision = registry.apply_correlation_gate(factor_id)
                status = registry.get_factor(factor_id)["status"]
                if not decision.passed and status == FactorStatus.CORR_REJECTED:
                    summary["corr_rejected"] += 1
                elif status == FactorStatus.PASSED:
                    summary["passed"] += 1
                else:
                    summary["simulated"] += 1
            elif result.passed is False:
                summary["metric_rejected"] += 1
            else:
                summary["simulated"] += 1

    log.info(
        "historical migration from %s: %s", source_path, json.dumps(summary)
    )
    return summary


# --------------------------------------------------------------------------- #
# Live account import
# --------------------------------------------------------------------------- #
def _remote_alpha_to_result(item: dict[str, Any]) -> SimpleNamespace:
    """Adapt a normalized /users/self/alphas item to the save_* duck contract."""
    metrics = item.get("metrics") or {}
    settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
    return SimpleNamespace(
        expression=item["expression"],
        settings_json=json.dumps(settings, sort_keys=True),
        status=SimulationStatus.COMPLETED,
        remote_alpha_id=item.get("alpha_id"),
        simulation_id=None,
        registry_source="account_import",
        passed=True,
        reasons=[],
        sharpe=metrics.get("sharpe"),
        fitness=metrics.get("fitness"),
        turnover=metrics.get("turnover"),
        returns=metrics.get("returns"),
        drawdown=metrics.get("drawdown"),
        margin=metrics.get("margin"),
        pnl=metrics.get("pnl"),
        book_size=metrics.get("book_size"),
        long_count=metrics.get("long_count"),
        short_count=metrics.get("short_count"),
        grade=item.get("grade"),
        stage=item.get("stage"),
        test_stats=item.get("test") if isinstance(item.get("test"), dict) else None,
        checks=item.get("checks") or {},
        submission_checks=None,
        self_correlation=None,
        error=None,
    )


def import_submitted_factors(
    client: Any,
    registry: FactorRegistry,
    *,
    max_pages: int | None = None,
    log: Any = None,
) -> dict[str, int]:
    """Fetch the account's live alphas and upsert them as SUBMITTED.

    Idempotent on ``brain_alpha_id`` / ``exact_hash``: re-running refreshes
    metrics but never duplicates a row or inflates submission counters.
    """
    log = log or get_logger("registry.importer")
    client.ensure_authenticated()
    page_items = client.list_self_alphas(max_pages=max_pages)

    summary = {"fetched": 0, "imported": 0, "updated": 0, "skipped_no_expression": 0}
    for item in page_items:
        summary["fetched"] += 1
        expression = item.get("expression")
        if not expression:
            summary["skipped_no_expression"] += 1
            continue
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        existing = registry.find_exact(expression, settings)
        was_submitted = bool(
            existing and existing["status"] == FactorStatus.SUBMITTED
        )

        adapter = _remote_alpha_to_result(item)
        factor_id = registry.save_simulation_result(adapter)
        registry.mark_submitted(
            factor_id, brain_alpha_id=str(item.get("alpha_id") or "") or None
        )
        if was_submitted:
            summary["updated"] += 1
        else:
            summary["imported"] += 1

    log.info("submitted-alpha import: %s", json.dumps(summary))
    return summary
