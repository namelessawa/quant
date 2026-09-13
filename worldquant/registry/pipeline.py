"""Unified research pipeline: one gate between idea generation and BRAIN.

Every research entry point (``run_backtest.py``, ``search_alpha.py`` and any
future generator) must go through the same three operations — this module is
the only orchestration seam:

1. :func:`gate_specs` — pre-simulation Registry gate for a whole batch. It runs
   *before* the :class:`~worldquant.storage.ResultStore` execution-dedup filter,
   so every candidate is recorded in research memory even if the local store
   later decides the simulation itself does not need to run.
2. :func:`record_results` — funnel finished :class:`~worldquant.models.AlphaResult`
   objects back into the Registry, including the per-neighbor correlation rows
   already parsed from BRAIN's submission check (never re-requested).
3. :func:`evaluate_acceptance` / :class:`AcceptanceDecision` — the single
   research acceptance rule: simulation completed AND grade clears the target AND
   BRAIN's eight submission checks pass AND the *research* correlation gate
   (stricter than BRAIN's official 0.70) is PASS. UNKNOWN corr evidence rejects
   by default; it is never a vacuous pass.

Nothing in here performs network calls or simulations, and nothing raises into
the caller's hot loop — Registry bookkeeping failures degrade to "no gate",
never to a crashed research run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..api import SimulationStatus
from ..logging_utils import get_logger
from .adapter import gate_candidate, record_completed
from .config import CorrelationConfig
from .correlation import CorrelationDecision, CorrelationStatus
from .store import FactorRegistry, FactorStatus


@dataclass(frozen=True)
class GateBatchResult:
    """Outcome of gating one candidate batch."""

    kept: list[Any]
    #: action -> number of blocked candidates
    tally: dict[str, int]

    @property
    def blocked(self) -> int:
        return sum(self.tally.values())


def gate_specs(
    registry: FactorRegistry | None,
    specs: Sequence[Any],
    *,
    force: bool = False,
    log: Any = None,
) -> GateBatchResult:
    """Run the Registry pre-simulation gate over ``specs``.

    Each spec only needs the ``AlphaSpec`` duck-typed attributes
    (``expression`` / ``settings`` and, optionally, ``source`` / ``force_gate``
    / ``ablation_group_id`` / ``changed_parameters`` /
    ``parent_experiment_id``). When ``registry`` is ``None`` (disabled or
    failed to open) every spec passes through unchanged, preserving the
    pre-Registry execution path exactly.
    """
    if registry is None:
        return GateBatchResult(kept=list(specs), tally={})

    log = log or registry.log
    kept: list[Any] = []
    tally: dict[str, int] = {}
    for spec in specs:
        _, decision = gate_candidate(
            registry,
            spec.expression,
            spec.settings,
            source=getattr(spec, "source", None),
            force=force or bool(getattr(spec, "force_gate", False)),
            ablation_group_id=getattr(spec, "ablation_group_id", None),
            changed_parameters=getattr(spec, "changed_parameters", None),
            parent_experiment_id=getattr(spec, "parent_experiment_id", None),
        )
        if decision is None or decision.passed:
            kept.append(spec)
        else:
            tally[decision.action] = tally.get(decision.action, 0) + 1
    if tally:
        log.info(
            "Registry gate skipped %d/%d candidate(s): %s",
            sum(tally.values()), len(specs), tally,
        )
    return GateBatchResult(kept=kept, tally=tally)


def record_results(
    registry: FactorRegistry | None,
    results: Iterable[Any],
) -> list[tuple[Any, dict[str, Any] | None]]:
    """Fold finished results into research memory.

    Returns ``(result, outcome)`` pairs; ``outcome`` is ``None`` when the
    Registry is disabled. The per-neighbor correlations carried on the result
    are stored as pairwise SELF edges; without them only the SELF_MAX aggregate
    is available and the corr verdict stays UNKNOWN.
    """
    recorded: list[tuple[Any, dict[str, Any] | None]] = []
    for result in results:
        outcome: dict[str, Any] | None = None
        if registry is not None:
            outcome = record_completed(registry, result)
        recorded.append((result, outcome))
    return recorded


# --------------------------------------------------------------------------- #
# Acceptance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AcceptanceDecision:
    """The unified research verdict for one completed simulation."""

    accepted: bool
    completed: bool
    grade_ok: bool
    #: BRAIN's own verdict: all eight submission checks PASS (official
    #: SELF_CORRELATION limit 0.70 included).
    submittable: bool
    #: Registry research corr verdict: PASS / FAIL / UNKNOWN / None (no registry
    #: or corr gate disabled).
    research_corr_status: str | None
    research_corr_band: str | None
    max_self_correlation: float | None
    research_threshold: float | None
    official_threshold: float | None
    corr_margin: float | None
    reason: str


def _research_verdict(
    outcome: dict[str, Any] | None,
) -> tuple[str | None, CorrelationDecision | None]:
    """Derive PASS/FAIL/UNKNOWN/NOT_APPLICABLE from a handle_completed outcome."""
    if not outcome:
        return None, None
    # The stored corr_status column is the authoritative verdict marker and
    # survives even when this call did not re-run the gate (e.g. a historical
    # alpha re-evaluated via --include-existing). Any persisted verdict wins
    # directly: a SUBMITTED factor recorded as PASS/FAIL/UNKNOWN must NOT be
    # re-derived from its lifecycle state (SUBMITTED is neither PASSED nor
    # SIMULATED, so the lifecycle fallback would wrongly report "not
    # evaluated"). The decision object (when present) is still returned so
    # describe_corr_verdicts can render band/margin for fresh gate runs.
    stored_corr = outcome.get("corr_status")
    known = {
        CorrelationStatus.PASS.value,
        CorrelationStatus.FAIL.value,
        CorrelationStatus.UNKNOWN.value,
        CorrelationStatus.NOT_APPLICABLE.value,
    }
    if stored_corr in known:
        decision = outcome.get("correlation_decision")
        return stored_corr, (
            decision if isinstance(decision, CorrelationDecision) else None
        )
    # No persisted verdict: fall back to the in-memory decision object (fresh
    # gate run) or infer from the lifecycle status.
    decision = outcome.get("correlation_decision")
    if isinstance(decision, CorrelationDecision):
        return decision.status.value, decision
    # No decision object: apply_correlation_gate was skipped (no evidence yet,
    # or gate disabled). A still-SIMULATED factor with no decision means
    # UNKNOWN corr evidence; other statuses speak for themselves.
    status = outcome.get("status")
    if status == FactorStatus.SIMULATED:
        return CorrelationStatus.UNKNOWN.value, None
    if status == FactorStatus.CORR_REJECTED:
        return CorrelationStatus.FAIL.value, None
    if status == FactorStatus.PASSED:
        return CorrelationStatus.PASS.value, None
    return None, None


def evaluate_acceptance(
    result: Any,
    outcome: dict[str, Any] | None,
    *,
    grade_ok: bool,
    allow_unknown: bool = False,
    corr_config: CorrelationConfig | None = None,
) -> AcceptanceDecision:
    """Apply the single research acceptance rule.

    ``grade_ok`` is the caller's grade-floor comparison (kept here so the
    pipeline does not depend on a specific grade enum). ``outcome`` is the dict
    returned by :meth:`FactorRegistry.handle_completed` /
    :func:`record_completed`. With no Registry outcome, research corr is
    ``None`` and acceptance can only succeed when ``allow_unknown`` is set —
    UNKNOWN is never silently accepted. When the local correlation gate is
    disabled in config (``correlation.enabled=false``) the research verdict is
    NOT_APPLICABLE: it adds no rejection and acceptance falls back to BRAIN's
    official submission checks. NOT_APPLICABLE is NOT the same as UNKNOWN —
    nothing is pending and no evidence is required.
    """
    cfg = corr_config or CorrelationConfig()
    completed = result.status == SimulationStatus.COMPLETED
    submittable = bool(getattr(result, "is_submittable", False))
    corr_status, decision = _research_verdict(outcome)
    self_corr = getattr(result, "self_correlation", None)
    # Disabled-by-config gate ⇒ NOT_APPLICABLE even for rows recorded before
    # the explicit status existed (historical outcomes carry no marker).
    if not cfg.enabled and corr_status not in (
        CorrelationStatus.FAIL.value,
        CorrelationStatus.NOT_APPLICABLE.value,
    ):
        corr_status = CorrelationStatus.NOT_APPLICABLE.value
        decision = None

    reasons: list[str] = []
    if not completed:
        reasons.append(f"simulation {result.status}")
    if not grade_ok:
        reasons.append(f"grade {result.grade or 'NONE'} below target")
    if not submittable:
        reasons.append("BRAIN submission checks not all PASS")
    corr_pass = corr_status == CorrelationStatus.PASS.value
    corr_skipped = corr_status == CorrelationStatus.NOT_APPLICABLE.value
    if corr_status == CorrelationStatus.FAIL.value:
        margin = decision.corr_margin if decision else None
        reasons.append(
            f"research correlation FAIL"
            + (f" (margin {margin:+.4f})" if margin is not None else "")
        )
    elif corr_status == CorrelationStatus.UNKNOWN.value and not allow_unknown:
        reasons.append("research correlation UNKNOWN (no verified evidence)")
    elif corr_status is None and not allow_unknown:
        reasons.append("research correlation not evaluated")

    accepted = completed and grade_ok and submittable and (
        corr_pass or corr_skipped or (
            corr_status in (CorrelationStatus.UNKNOWN.value, None)
            and allow_unknown
        )
    )

    return AcceptanceDecision(
        accepted=accepted,
        completed=completed,
        grade_ok=grade_ok,
        submittable=submittable,
        research_corr_status=corr_status,
        research_corr_band=(decision.band.value if decision else None),
        max_self_correlation=self_corr,
        research_threshold=(decision.threshold if decision else cfg.research_threshold),
        official_threshold=cfg.official_threshold,
        corr_margin=(decision.corr_margin if decision else None),
        reason="; ".join(reasons) if reasons else "all gates PASS",
    )


def describe_corr_verdicts(
    result: Any,
    outcome: dict[str, Any] | None,
    *,
    corr_config: CorrelationConfig | None = None,
) -> str:
    """Render BOTH correlation verdicts on one line.

    BRAIN's official submission check (limit 0.70) and the Registry's stricter
    research gate (default 0.65) are deliberately separate rules, and a value
    like 0.68 passes the first but fails the second — that gap must be visible
    in every rejection log.
    """
    cfg = corr_config or CorrelationConfig()
    self_corr = getattr(result, "self_correlation", None)
    corr_text = f"{self_corr:.4f}" if self_corr is not None else "n/a"
    if getattr(result, "submission_checks", None):
        brain = "PASS" if result.is_submittable else "FAIL"
    else:
        brain = "NOT_CHECKED"
    brain_part = f"BRAIN(official>={cfg.official_threshold:.2f}): {brain} self-corr={corr_text}"

    corr_status, decision = _research_verdict(outcome)
    if not cfg.enabled and corr_status != CorrelationStatus.FAIL.value:
        research_part = (
            f"RESEARCH(<{cfg.research_threshold:.2f}): NOT_APPLICABLE (gate disabled)"
        )
    elif corr_status is None:
        research_part = f"RESEARCH(<{cfg.research_threshold:.2f}): NOT_EVALUATED"
    elif decision is not None:
        margin = (
            f"{decision.corr_margin:+.4f}"
            if decision.corr_margin is not None else "n/a"
        )
        research_part = (
            f"RESEARCH(<{cfg.research_threshold:.2f}): {corr_status} "
            f"band={decision.band.value} margin={margin}"
        )
    else:
        research_part = f"RESEARCH(<{cfg.research_threshold:.2f}): {corr_status}"
    return f"{brain_part} | {research_part}"


def get_logger_safe() -> Any:
    """Module logger for callers without their own."""
    return get_logger("registry.pipeline")
