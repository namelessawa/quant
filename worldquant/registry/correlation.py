"""Post-simulation correlation gate.

This is the *research/submission* gate, deliberately separate from BRAIN's own
``SELF_CORRELATION`` submission check (which is parsed independently in
:mod:`worldquant.api` and stored verbatim on the metrics row).

Two rules coexist by design:

* Research similarity uses ``abs(corr)`` when
  ``CorrelationConfig.use_absolute_value`` is true (the default): a -0.90 alpha
  is just as redundant as a +0.90 one.
* The official BRAIN verdict returned by ``GET /alphas/{id}/check`` is never
  rewritten by that flag. It lives in ``factor_metrics.checks_*`` and in the
  per-neighbor correlation rows with their original sign.

Only correlations against the *protected set* (SUBMITTED alphas, PASSED
candidates, and unresolved BRAIN neighbors, which are always live account
alphas) are evaluated. Research-set rows feed de-dup/novelty instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .config import CorrelationConfig


@dataclass(frozen=True)
class CorrelationDecision:
    """The verdict for one candidate against its protected neighbors."""

    passed: bool
    max_corr: float | None
    max_abs_corr: float | None
    threshold: float
    reason: str
    nearest_factor_id: int | None = None
    nearest_brain_alpha_id: str | None = None
    use_absolute_value: bool = True
    evaluated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "max_corr": self.max_corr,
            "max_abs_corr": self.max_abs_corr,
            "threshold": self.threshold,
            "reason": self.reason,
            "nearest_factor_id": self.nearest_factor_id,
            "nearest_brain_alpha_id": self.nearest_brain_alpha_id,
            "use_absolute_value": self.use_absolute_value,
            "evaluated": self.evaluated,
        }


class CorrelationGate:
    """Compare stored correlation records against a configurable cutoff."""

    def __init__(self, config: CorrelationConfig | None = None) -> None:
        self.config = config or CorrelationConfig()

    def evaluate(
        self,
        factor_id: int,
        records: Sequence[dict[str, Any]],
    ) -> CorrelationDecision:
        """Return the worst-neighbour decision.

        ``records`` are dicts carrying at least ``correlation`` and optionally
        ``other_factor_id`` / ``other_brain_alpha_id``. The caller
        (:meth:`FactorRegistry.apply_correlation_gate`) is responsible for
        filtering them down to the protected set; this gate only compares.
        """
        threshold = float(self.config.threshold)
        use_abs = bool(self.config.use_absolute_value)

        max_corr: float | None = None
        max_abs_corr: float | None = None
        nearest_factor_id: int | None = None
        nearest_brain: str | None = None
        worst_magnitude: float | None = None

        evaluated = 0
        for record in records:
            value = record.get("correlation")
            if value is None:
                value = record.get("abs_correlation")
            try:
                corr = float(value)
            except (TypeError, ValueError):
                continue
            evaluated += 1
            magnitude = abs(corr) if use_abs else corr

            if worst_magnitude is None or magnitude > worst_magnitude:
                worst_magnitude = magnitude
                max_corr = corr
                max_abs_corr = abs(corr)
                nearest_factor_id = record.get("other_factor_id")
                nearest_brain = record.get("other_brain_alpha_id")

        if worst_magnitude is None:
            return CorrelationDecision(
                passed=True,
                max_corr=None,
                max_abs_corr=None,
                threshold=threshold,
                reason="no protected-set correlations available; gate vacuously passed",
                nearest_factor_id=None,
                nearest_brain_alpha_id=None,
                use_absolute_value=use_abs,
                evaluated=0,
            )

        signed_note = "" if use_abs else " (signed mode: negative correlations do not block)"
        if worst_magnitude > threshold:
            who = nearest_brain or (
                f"factor#{nearest_factor_id}" if nearest_factor_id is not None else "unknown alpha"
            )
            reason = (
                f"max correlation {worst_magnitude:.4f} against {who} exceeds "
                f"cutoff {threshold:.2f}{signed_note}"
            )
            return CorrelationDecision(
                passed=False,
                max_corr=max_corr,
                max_abs_corr=max_abs_corr,
                threshold=threshold,
                reason=reason,
                nearest_factor_id=nearest_factor_id,
                nearest_brain_alpha_id=nearest_brain,
                use_absolute_value=use_abs,
                evaluated=evaluated,
            )

        return CorrelationDecision(
            passed=True,
            max_corr=max_corr,
            max_abs_corr=max_abs_corr,
            threshold=threshold,
            reason=(
                f"max correlation {worst_magnitude:.4f} within cutoff "
                f"{threshold:.2f}{signed_note}"
            ),
            nearest_factor_id=nearest_factor_id,
            nearest_brain_alpha_id=nearest_brain,
            use_absolute_value=use_abs,
            evaluated=evaluated,
        )
