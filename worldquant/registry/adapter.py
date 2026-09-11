"""Opt-in glue between the backtest pipeline and the Factor Registry.

The registry never re-implements simulation or correlation; this module only
wires existing objects together:

* :func:`open_registry` builds a :class:`FactorRegistry` from an
  :class:`~worldquant.config.AppConfig` (or returns ``None`` when disabled).
* :func:`gate_candidate` evaluates the pre-simulation duplicate/novelty gate
  and records the decision on the candidate row.
* :func:`record_completed` funnels an
  :class:`~worldquant.models.AlphaResult` through
  :meth:`FactorRegistry.handle_completed`.

Every function is defensive: registry bookkeeping must never break a live
backtest run, so callers can use them unconditionally inside ``try`` blocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from .gates import (
    ACTION_REJECT_EXACT,
    ACTION_SKIP_LOW_NOVELTY,
    ACTION_SKIP_SIGNAL_DUPLICATE,
    PreSimulationGateResult,
    pre_simulation_gate,
)
from .store import FactorRegistry, FactorStatus


def load_field_datasets(catalog_path: str | Path | None) -> dict[str, str] | None:
    """Read ``{field: dataset}`` from the cached field catalog, or None."""
    if not catalog_path:
        return None
    path = Path(catalog_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return {str(k): str(v) for k, v in payload.items()}
    return None


def open_registry(config: Any, *, log: Any = None) -> FactorRegistry | None:
    """Open the registry configured on ``AppConfig``; None when disabled.

    ``config`` only needs ``config.registry`` and
    ``config.storage.field_catalog_path``, so tests pass lightweight fakes.
    """
    log = log or get_logger("registry.adapter")
    registry_config = getattr(config, "registry", None)
    if registry_config is None or not getattr(registry_config, "enabled", False):
        log.debug("factor registry disabled by config")
        return None

    storage = getattr(config, "storage", None)
    catalog_path = getattr(storage, "field_catalog_path", None)
    field_datasets = load_field_datasets(catalog_path)
    try:
        return FactorRegistry(
            registry_config.db_path,
            registry_config,
            field_datasets=field_datasets,
            logger=log,
        )
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        log.warning("could not open factor registry at %s: %s",
                    getattr(registry_config, "db_path", "?"), exc)
        return None


def gate_candidate(
    registry: FactorRegistry,
    expression: str,
    settings: dict[str, Any],
    *,
    source: str | None = None,
    force: bool = False,
    ablation_group_id: str | None = None,
    changed_parameters: dict[str, Any] | None = None,
    parent_experiment_id: int | None = None,
) -> tuple[int | None, PreSimulationGateResult | None]:
    """Register a candidate and run the pre-simulation gate.

    Returns ``(factor_id, decision)``. A rejected/skipped candidate has its
    lifecycle status recorded already (``DUPLICATE`` — the exact row for an
    exact match, or a novelty/signal-saturation skip tagged in the reason).
    Returns ``(None, None)`` when bookkeeping itself fails, in which case the
    caller should not block the simulation.

    Pass ``source='ablation'`` (or ``ablation_group_id``) for deliberate
    parameter sweeps: same-signal skips are then suppressed by design.
    """
    try:
        candidate = registry.register_candidate(
            expression,
            settings,
            source=source or "generator",
            ablation_group_id=ablation_group_id,
            changed_parameters=changed_parameters,
            parent_experiment_id=parent_experiment_id,
        )
        factor_id = candidate.factor_id
        decision = pre_simulation_gate(
            registry, expression, settings, source=source, force=force
        )
        if not decision.passed:
            if decision.action == ACTION_REJECT_EXACT:
                registry.mark_duplicate(
                    factor_id, f"exact duplicate: {decision.reason}"
                )
            elif decision.action == ACTION_SKIP_SIGNAL_DUPLICATE:
                # Same canonical expression already explored with different
                # settings; the message steers toward new data/legs, not sweeps.
                registry.mark_duplicate(
                    factor_id, f"same signal skip: {decision.reason}"
                )
            elif decision.action == ACTION_SKIP_LOW_NOVELTY:
                # No dedicated status for this; record it as a pre-simulation
                # duplicate-class decision with the novelty reason preserved.
                registry.mark_duplicate(
                    factor_id, f"low novelty skip: {decision.reason}"
                )
        # Stash the score for ranking/reporting.
        if decision.novelty is not None:
            registry.set_novelty_score(factor_id, float(decision.novelty.score))
        return factor_id, decision
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        registry.log.warning("registry pre-simulation gate failed: %s", exc)
        return None, None


def record_completed(
    registry: FactorRegistry,
    result: Any,
    *,
    corr_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist a finished (or failed) simulation via the registry adapter.

    Thin wrapper around :meth:`FactorRegistry.handle_completed`, kept as a
    function so scripts have a single import and a stable call shape.
    """
    return registry.handle_completed(result, corr_records=corr_records)


def mark_submission_outcome(
    registry: FactorRegistry,
    result: Any,
    *,
    factor_id: int | None = None,
) -> None:
    """Record SUBMITTED / SUBMIT_FAILED for a result that passed the gate.

    Uses the stored brain alpha id to find the row when ``factor_id`` is not
    known. Never raises.
    """
    try:
        if factor_id is None:
            alpha_id = getattr(result, "remote_alpha_id", None)
            if not alpha_id:
                return
            rows = registry._query(
                "SELECT id FROM factors WHERE brain_alpha_id = ?", (str(alpha_id),)
            )
            if not rows:
                return
            factor_id = int(rows[0]["id"])
        if result.is_submittable:
            registry.mark_submitted(
                factor_id, brain_alpha_id=getattr(result, "remote_alpha_id", None)
            )
        else:
            reason = "; ".join(result.submission_failures or []) or "submission checks failed"
            registry.mark_submit_failed(factor_id, reason[:500])
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        registry.log.warning("registry submission bookkeeping failed: %s", exc)
