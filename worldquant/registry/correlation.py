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

The verdict is four-valued: ``PASS`` / ``FAIL`` / ``UNKNOWN`` /
``NOT_APPLICABLE``. Missing, pending, timed-out or unparsable records produce
``UNKNOWN`` — never a vacuous pass — so an alpha with no evidence cannot be
promoted automatically; ``NOT_APPLICABLE`` means the local gate is disabled in
config (``correlation.enabled=false``), so no local verdict is required and
acceptance falls back to BRAIN's official checks.
``allow_submit_without_corr`` (default false) governs whether manual override
is permitted for UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from .config import CorrelationConfig


class CorrelationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: No usable evidence (empty / pending / timeout / malformed records).
    UNKNOWN = "UNKNOWN"
    #: The local research gate is switched off by config
    #: (``correlation.enabled=false``). This is deliberately distinct from
    #: UNKNOWN: nothing is pending and nothing is missing — acceptance simply
    #: falls back to BRAIN's official submission checks.
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CorrelationBand(str, Enum):
    #: Well clear of every limit (below the warning threshold).
    DIVERSE = "DIVERSE"
    #: Close enough to the protected set to be worth attention.
    WARNING = "WARNING"
    #: At/above the research cutoff — research-rejected.
    RESEARCH_REJECT = "RESEARCH_REJECT"
    #: No usable evidence.
    UNKNOWN = "UNKNOWN"
    #: Local research correlation gate disabled.
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CorrelationDecision:
    """The verdict for one candidate against its protected neighbors."""

    passed: bool
    max_corr: float | None
    max_abs_corr: float | None
    threshold: float
    reason: str
    status: CorrelationStatus = CorrelationStatus.UNKNOWN
    band: CorrelationBand = CorrelationBand.UNKNOWN
    #: ``research_threshold - max_abs_corr``; larger = safer. None when UNKNOWN.
    corr_margin: float | None = None
    #: True when the margin is within ``margin_warning`` of the limit.
    very_close_to_limit: bool = False
    nearest_factor_id: int | None = None
    nearest_brain_alpha_id: str | None = None
    use_absolute_value: bool = True
    evaluated: int = 0
    #: Whether submission may proceed in the current state (UNKNOWN blocks it
    #: unless ``allow_submit_without_corr`` is explicitly enabled).
    submission_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status.value,
            "band": self.band.value,
            "max_corr": self.max_corr,
            "max_abs_corr": self.max_abs_corr,
            "threshold": self.threshold,
            "research_threshold": self.threshold,
            "corr_margin": self.corr_margin,
            "very_close_to_limit": self.very_close_to_limit,
            "reason": self.reason,
            "nearest_factor_id": self.nearest_factor_id,
            "nearest_brain_alpha_id": self.nearest_brain_alpha_id,
            "use_absolute_value": self.use_absolute_value,
            "evaluated": self.evaluated,
            "submission_allowed": self.submission_allowed,
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
        ``other_factor_id`` / ``other_brain_alpha_id`` / ``status``. The caller
        (:meth:`FactorRegistry.apply_correlation_gate`) is responsible for
        filtering them down to the protected set; this gate only compares.
        """
        research_threshold = float(self.config.research_threshold)
        warning_threshold = float(self.config.warning_threshold)
        margin_warning = float(self.config.margin_warning)
        use_abs = bool(self.config.use_absolute_value)

        max_corr: float | None = None
        max_abs_corr: float | None = None
        nearest_factor_id: int | None = None
        nearest_brain: str | None = None
        worst_magnitude: float | None = None

        evaluated = 0
        pending = 0
        for record in records:
            status_hint = str(record.get("status") or "").upper()
            value = record.get("correlation")
            if value is None:
                value = record.get("abs_correlation")
            try:
                corr = float(value)
            except (TypeError, ValueError):
                if status_hint and status_hint != "COMPLETED":
                    pending += 1
                continue
            # An explicit PENDING/IN_PROGRESS/TIMEOUT/ERROR marker means the
            # numeric payload (if any) is not final evidence.
            if status_hint in {"PENDING", "IN_PROGRESS", "TIMEOUT", "ERROR", "FAILED"}:
                pending += 1
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
            why = (
                "correlation checks still pending/incomplete"
                if pending
                else "no protected-set correlations available"
            )
            return CorrelationDecision(
                passed=False,
                status=CorrelationStatus.UNKNOWN,
                band=CorrelationBand.UNKNOWN,
                max_corr=None,
                max_abs_corr=None,
                threshold=research_threshold,
                corr_margin=None,
                very_close_to_limit=False,
                reason=f"{why}; gate is UNKNOWN (not a pass)",
                nearest_factor_id=None,
                nearest_brain_alpha_id=None,
                use_absolute_value=use_abs,
                evaluated=0,
                submission_allowed=bool(self.config.allow_submit_without_corr),
            )

        corr_margin = research_threshold - worst_magnitude
        very_close = 0.0 <= corr_margin <= margin_warning
        signed_note = "" if use_abs else " (signed mode: negative correlations do not block)"

        if worst_magnitude >= research_threshold:
            who = nearest_brain or (
                f"factor#{nearest_factor_id}" if nearest_factor_id is not None else "unknown alpha"
            )
            reason = (
                f"max correlation {worst_magnitude:.4f} against {who} reaches/exceeds "
                f"research cutoff {research_threshold:.2f} (margin {corr_margin:+.4f})"
                f"{signed_note}"
            )
            return CorrelationDecision(
                passed=False,
                status=CorrelationStatus.FAIL,
                band=CorrelationBand.RESEARCH_REJECT,
                max_corr=max_corr,
                max_abs_corr=max_abs_corr,
                threshold=research_threshold,
                corr_margin=corr_margin,
                very_close_to_limit=very_close,
                reason=reason,
                nearest_factor_id=nearest_factor_id,
                nearest_brain_alpha_id=nearest_brain,
                use_absolute_value=use_abs,
                evaluated=evaluated,
                submission_allowed=False,
            )

        if worst_magnitude >= warning_threshold:
            band = CorrelationBand.WARNING
        else:
            band = CorrelationBand.DIVERSE
        close_note = "  VERY_CLOSE_TO_LIMIT" if very_close else ""
        return CorrelationDecision(
            passed=True,
            status=CorrelationStatus.PASS,
            band=band,
            max_corr=max_corr,
            max_abs_corr=max_abs_corr,
            threshold=research_threshold,
            corr_margin=corr_margin,
            very_close_to_limit=very_close,
            reason=(
                f"max correlation {worst_magnitude:.4f} within research cutoff "
                f"{research_threshold:.2f} (margin {corr_margin:+.4f}); "
                f"band={band.value}{signed_note}{close_note}"
            ),
            nearest_factor_id=nearest_factor_id,
            nearest_brain_alpha_id=nearest_brain,
            use_absolute_value=use_abs,
            evaluated=evaluated,
            submission_allowed=True,
        )
