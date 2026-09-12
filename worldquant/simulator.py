"""Simulation orchestration: submit, poll, collect, filter, persist.

Responsibilities kept separate on purpose:
    * :class:`~worldquant.client.WorldQuantClient` only speaks HTTP,
    * :class:`~worldquant.storage.ResultStore` only persists,
    * :class:`SimulationRunner` owns the lifecycle and the resume logic.

Resume model: every alpha is keyed by ``sha256(expression + settings)``. Before
submitting, the runner looks for (1) an already-completed result — skip it, or
(2) a simulation that was submitted but never reached a terminal state — pick up
polling its remote id instead of submitting a duplicate. State is written to
SQLite at every transition, so a kill -9 mid-batch loses no task.
"""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence

from .api import SimulationStatus
from .client import WorldQuantClient
from .config import AppConfig
from .exceptions import (
    APIError,
    AuthError,
    CaptchaRequiredError,
    SimulationError,
    SimulationFailedError,
    SimulationTimeoutError,
    WorldQuantError,
)
from .filters import evaluate_filters
from .hashing import dedup_key
from .loader import specs_from_expressions
from .logging_utils import get_logger
from .models import AlphaResult, AlphaSpec
from .storage import ResultStore, utcnow_iso

#: Never poll faster than this, even if the server sends ``Retry-After: 0``.
MIN_POLL_DELAY = 2.0
#: Never sleep longer than this in one go, so ``max_wait`` stays meaningful.
MAX_POLL_DELAY = 60.0

#: Warn when the held-out test year keeps less than this fraction of the
#: in-sample sharpe. BRAIN grades on the full IS window, so without this an
#: alpha can look GOOD while its unseen year barely works.
OVERFIT_RATIO_THRESHOLD = 0.5

#: Re-check allowance for a simulation that already timed out once. It has
#: already consumed a full ``max_wait``, so give it a couple of status checks
#: rather than stalling the run for another full budget.
RESUME_TIMEOUT_BUDGET = 30.0


class SimulationRunner:
    """Runs alphas end-to-end and keeps the local database authoritative."""

    def __init__(
        self,
        client: WorldQuantClient,
        store: ResultStore,
        config: AppConfig,
        *,
        logger: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        experiment_log: Any = None,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self.log = logger or get_logger("runner")
        self.experiment_log = experiment_log
        self._sleep = sleep
        self._clock = clock

        self._halt = threading.Event()
        self._halt_reason: str | None = None
        self._counter_lock = threading.Lock()
        self._submitted_count = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @property
    def halted(self) -> bool:
        return self._halt.is_set()

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    def run_alpha(
        self,
        expression: str,
        settings: dict[str, Any],
        *,
        name: str | None = None,
        force: bool = False,
    ) -> AlphaResult:
        """Run a single expression. See :meth:`run_spec`."""
        spec = AlphaSpec(expression=expression, name=name, settings=dict(settings or {}))
        return self.run_spec(spec, force=force)

    def run_spec(self, spec: AlphaSpec, *, force: bool = False) -> AlphaResult:
        """Run one alpha through the full lifecycle, persisting every transition.

        Failures are recorded and returned rather than raised, so one bad alpha
        cannot abort a batch. Authentication failures are the exception: they
        halt the batch, because every remaining alpha would fail identically.
        """
        settings = self._effective_settings(spec)
        key = dedup_key(spec.expression, settings)
        label = spec.label

        if self._halt.is_set():
            return self._halted_result(spec)

        alpha_row_id = self.store.upsert_alpha(spec.expression, settings, name=spec.name)

        if not force:
            existing = self.store.find_completed_result(key)
            if existing is not None:
                self.log.info(
                    "Skipping %s: identical expression+settings already completed "
                    "(alpha=%s). Use --force to re-run.",
                    label, existing.remote_alpha_id or "unknown",
                )
                return existing

        resumable = None if force else self.store.find_resumable_simulation(alpha_row_id)
        remote_simulation_id: str | None = None

        if resumable and resumable.get("remote_simulation_id"):
            remote_simulation_id = str(resumable["remote_simulation_id"])
            self.log.info(
                "Resuming %s from simulation %s (status=%s)",
                label, remote_simulation_id, resumable.get("status"),
            )
            return self._await_and_collect(
                spec, settings, key, int(resumable["id"]), remote_simulation_id, label
            )

        # Nothing to poll. Reuse an orphaned row (created before the POST
        # returned) rather than adding another, so it cannot linger forever and
        # be rediscovered by every resume_incomplete call.
        simulation_row_id = (
            int(resumable["id"]) if resumable else self.store.create_simulation(alpha_row_id)
        )

        try:
            self.client.ensure_authenticated()
            remote_simulation_id = self.client.create_simulation(spec.expression, settings)
        except CaptchaRequiredError as exc:
            # Subclass of AuthError, so it must be handled first — and it blocks
            # every remaining alpha, not just this one.
            self._stop_batch(f"interactive verification required: {exc}")
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.AUTH_ERROR, str(exc),
            )
        except AuthError as exc:
            self._stop_batch(f"authentication failure while submitting {label}: {exc}")
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.AUTH_ERROR, str(exc),
            )
        except APIError as exc:
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.REQUEST_ERROR, str(exc),
            )

        self.store.update_simulation(
            simulation_row_id,
            status=SimulationStatus.SUBMITTED,
            remote_simulation_id=remote_simulation_id,
        )
        with self._counter_lock:
            self._submitted_count += 1
            submitted = self._submitted_count
        self.log.info("Alpha %s submitted: %s", submitted, remote_simulation_id)

        return self._await_and_collect(
            spec, settings, key, simulation_row_id, str(remote_simulation_id), label
        )

    def run_batch(
        self,
        alphas: Sequence[AlphaSpec | str],
        *,
        force: bool = False,
        limit: int | None = None,
    ) -> list[AlphaResult]:
        """Run many alphas with bounded concurrency.

        A per-alpha exception is captured and turned into a failed
        :class:`AlphaResult`; the batch continues. Only an authentication
        failure stops everything, via the internal halt flag.
        """
        specs = self._coerce_specs(alphas)
        if limit is not None and limit >= 0:
            specs = specs[:limit]
        if not specs:
            self.log.warning("run_batch called with no alphas")
            return []

        concurrency = max(1, int(self.config.runner.concurrency))
        total = len(specs)
        self.log.info(
            "Starting batch: %d alpha(s), concurrency=%d, poll=%.0fs, max_wait=%.0fs",
            total, concurrency, self.config.runner.poll_interval, self.config.runner.max_wait,
        )

        results: list[AlphaResult] = []
        if concurrency == 1:
            for index, spec in enumerate(specs, start=1):
                if self._halt.is_set():
                    results.append(self._halted_result(spec))
                    continue
                self.log.info("[%d/%d] %s", index, total, spec.label)
                results.append(self._safe_run(spec, force))
            return results

        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="wqsim"
        ) as executor:
            futures: dict[Future, tuple[int, AlphaSpec]] = {}
            not_submitted: list[tuple[int, AlphaSpec]] = []
            for index, spec in enumerate(specs, start=1):
                if self._halt.is_set():
                    # Keep enumerating so every remaining alpha is still
                    # accounted for in the results, rather than vanishing.
                    not_submitted.append((index, spec))
                    continue
                futures[executor.submit(self._safe_run, spec, force)] = (index, spec)

            ordered: dict[int, AlphaResult] = {}
            for future, (index, spec) in futures.items():
                try:
                    ordered[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolation boundary
                    self.log.error(
                        "Alpha %s raised an unexpected %s: %s",
                        spec.label, type(exc).__name__, exc,
                    )
                    ordered[index] = self._error_result(spec, exc)
            for index, spec in not_submitted:
                ordered[index] = self._halted_result(spec)
            results = [ordered[i] for i in sorted(ordered)]

        return results

    def resume_incomplete(self, *, force: bool = False) -> list[AlphaResult]:
        """Finish simulations left non-terminal by an earlier interrupted run."""
        pending = self.store.find_incomplete_simulations()
        if not pending:
            self.log.info("Nothing to resume: no incomplete simulations in the database")
            return []

        self.log.info("Resuming %d incomplete simulation(s)", len(pending))
        results: list[AlphaResult] = []
        for row in pending:
            if self._halt.is_set():
                break
            settings = self._settings_from_json(row.get("settings_json"))
            spec = AlphaSpec(
                expression=str(row.get("expression") or ""),
                name=row.get("name"),
                settings=settings,
            )
            remote_id = row.get("remote_simulation_id")
            simulation_row_id = int(row["simulation_row_id"])
            if not remote_id:
                # Submitted row without a remote id means we died between INSERT
                # and the POST returning. run_spec reuses that row when the dedup
                # key still resolves here — but if settings normalization changed
                # since the row was written, run_spec targets a different alpha
                # row and this one would be rediscovered on every startup.
                results.append(self.run_spec(spec))
                self._retire_if_still_pending(simulation_row_id)
                continue
            # A row that already burned a full max_wait and timed out gets only a
            # short re-check: the server may well have finished it, but waiting
            # another full budget can stall the whole run before any new work
            # starts. Rows interrupted mid-flight never got a budget, so they keep
            # the full one.
            prior_status = str(row.get("status") or "")
            resume_budget: float | None = None
            if prior_status == SimulationStatus.TIMEOUT:
                resume_budget = max(
                    RESUME_TIMEOUT_BUDGET, 2.0 * float(self.config.runner.poll_interval)
                )
                self.log.info(
                    "Simulation %s timed out before; re-checking with a %.0fs budget "
                    "instead of the full %.0fs",
                    remote_id, resume_budget, self.config.runner.max_wait,
                )

            results.append(
                self._await_and_collect(
                    spec, settings, str(row.get("dedup_key") or ""),
                    simulation_row_id, str(remote_id), spec.label,
                    max_wait=resume_budget,
                )
            )
        return results

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _coerce_specs(self, alphas: Iterable[AlphaSpec | str]) -> list[AlphaSpec]:
        specs: list[AlphaSpec] = []
        raw_strings: list[str] = []
        for item in alphas:
            if isinstance(item, AlphaSpec):
                specs.append(item)
            elif isinstance(item, str):
                raw_strings.append(item)
            else:
                raise TypeError(
                    f"run_batch accepts AlphaSpec or str, got {type(item).__name__}"
                )
        if raw_strings:
            specs.extend(specs_from_expressions(raw_strings, settings=self.config.settings))
        return specs

    def _settings_from_json(self, raw: Any) -> dict[str, Any]:
        if not raw:
            return dict(self.config.settings)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return dict(self.config.settings)
        return parsed if isinstance(parsed, dict) else dict(self.config.settings)

    def _safe_run(self, spec: AlphaSpec, force: bool) -> AlphaResult:
        """Run one spec, converting any escaping exception into a failed result."""
        try:
            return self.run_spec(spec, force=force)
        except CaptchaRequiredError as exc:
            self._stop_batch(f"interactive verification required: {exc}")
            return self._error_result(spec, exc, status=SimulationStatus.AUTH_ERROR)
        except AuthError as exc:
            self._stop_batch(f"authentication failure: {exc}")
            return self._error_result(spec, exc, status=SimulationStatus.AUTH_ERROR)
        except WorldQuantError as exc:
            self.log.error("Alpha %s failed: %s", spec.label, exc)
            return self._error_result(spec, exc)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self.log.error(
                "Alpha %s raised unexpected %s: %s", spec.label, type(exc).__name__, exc
            )
            return self._error_result(spec, exc)

    def _stop_batch(self, reason: str) -> None:
        if not self._halt.is_set():
            self._halt_reason = reason
            self._halt.set()
            self.log.error("Halting batch: %s", reason)

    def _effective_settings(self, spec: AlphaSpec) -> dict[str, Any]:
        """Per-alpha settings when present, otherwise the global config."""
        return spec.settings or self.config.settings

    def _halted_result(self, spec: AlphaSpec) -> AlphaResult:
        settings = self._effective_settings(spec)
        reason = self._halt_reason or "batch halted"
        self.log.warning("Skipping %s: %s", spec.label, reason)
        return AlphaResult(
            alpha_id=spec.name or spec.label,
            expression=spec.expression,
            dedup_key=dedup_key(spec.expression, settings),
            status=SimulationStatus.SKIPPED,
            settings_json=_dumps(settings),
            created_at=utcnow_iso(),
            error=f"not attempted: {reason}",
            passed=False,
            reasons=[f"SKIPPED: {reason}"],
        )

    def _await_and_collect(
        self,
        spec: AlphaSpec,
        settings: dict[str, Any],
        key: str,
        simulation_row_id: int,
        remote_simulation_id: str,
        label: str,
        *,
        max_wait: float | None = None,
    ) -> AlphaResult:
        """Poll to completion, then fetch and filter metrics."""
        try:
            remote_alpha_id = self._poll(
                remote_simulation_id, simulation_row_id, label, max_wait=max_wait
            )
        except CaptchaRequiredError as exc:
            self._stop_batch(f"interactive verification required: {exc}")
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.AUTH_ERROR, str(exc),
            )
        except AuthError as exc:
            self._stop_batch(f"authentication failure while polling {label}: {exc}")
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.AUTH_ERROR, str(exc),
            )
        except SimulationTimeoutError as exc:
            self.log.warning("Simulation %s timed out: %s", remote_simulation_id, exc)
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.TIMEOUT, str(exc),
                remote_simulation_id=remote_simulation_id,
            )
        except SimulationFailedError as exc:
            self.log.error("Simulation failed: %s", exc)
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.FAILED, str(exc),
                remote_simulation_id=remote_simulation_id,
            )
        except APIError as exc:
            self.log.error("Simulation %s request error: %s", remote_simulation_id, exc)
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.REQUEST_ERROR, str(exc),
                remote_simulation_id=remote_simulation_id,
            )

        self.log.info("Simulation %s completed", remote_simulation_id)

        try:
            alpha_data = self.client.get_alpha(remote_alpha_id)
        except (APIError, AuthError) as exc:
            if isinstance(exc, AuthError):
                self._stop_batch(f"authentication failure while fetching alpha: {exc}")
            return self._fail(
                spec, settings, key, simulation_row_id,
                SimulationStatus.REQUEST_ERROR,
                f"simulation completed but fetching alpha {remote_alpha_id} failed: {exc}",
                remote_simulation_id=remote_simulation_id,
                remote_alpha_id=remote_alpha_id,
            )

        yearly_stats: dict[str, dict[str, float | None]] = {}
        if self.config.fetch_yearly_stats:
            try:
                yearly_stats = self.client.fetch_yearly_stats(remote_alpha_id)
            except WorldQuantError as exc:
                self.log.warning("yearly stats unavailable for %s: %s", remote_alpha_id, exc)

        # The submission check is the acceptance gate: the grade follows fitness
        # alone, and SELF_CORRELATION is only ever resolved here.
        submission_checks: dict[str, Any] = {}
        self_correlation: float | None = None
        self_correlated_with: list[dict[str, Any]] | None = None
        checker = getattr(self.client, "check_submission", None)
        if callable(checker):
            try:
                checked = checker(remote_alpha_id)
            except WorldQuantError as exc:
                self.log.warning(
                    "submission check unavailable for %s: %s; treating it as NOT verified",
                    remote_alpha_id, exc,
                )
            else:
                if isinstance(checked, dict) and checked.get("total"):
                    submission_checks = checked.get("checks") or {}
                    self_correlation = checked.get("self_correlation")
                    neighbors = checked.get("self_correlated_with")
                    if isinstance(neighbors, list) and neighbors:
                        self_correlated_with = neighbors

        metrics = alpha_data.get("metrics") or {}
        result = AlphaResult(
            alpha_id=spec.name or label,
            expression=spec.expression,
            dedup_key=key,
            status=SimulationStatus.COMPLETED,
            simulation_id=remote_simulation_id,
            remote_alpha_id=str(alpha_data.get("alpha_id") or remote_alpha_id),
            settings_json=_dumps(settings),
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
            yearly_stats=yearly_stats,
            # The check endpoint resolves SELF_CORRELATION, which the plain alpha
            # payload leaves PENDING, so its copy wins when we have one.
            checks=submission_checks or (alpha_data.get("checks") or {}),
            raw_json=_dumps(alpha_data.get("raw")),
            grade=alpha_data.get("grade"),
            stage=alpha_data.get("stage"),
            train_stats=alpha_data.get("train"),
            test_stats=alpha_data.get("test"),
            submission_checks=submission_checks or None,
            self_correlation=self_correlation,
            self_correlated_with=self_correlated_with,
            created_at=utcnow_iso(),
            completed_at=utcnow_iso(),
        )

        outcome = evaluate_filters(result, self.config.filters, logger=self.log)
        result.passed = outcome.passed
        result.reasons = outcome.reasons

        self.store.save_result(simulation_row_id, result)
        self.store.update_simulation(
            simulation_row_id,
            status=SimulationStatus.COMPLETED,
            remote_alpha_id=result.remote_alpha_id,
            mark_completed=True,
        )

        self.log.info("%s", result.metrics_line())
        if result.submission_checks:
            if result.is_submittable:
                self.log.info(
                    "Submission checks: 8/8 PASS (self-correlation %s) - submittable",
                    f"{result.self_correlation:.4f}"
                    if result.self_correlation is not None else "n/a",
                )
            else:
                self.log.warning(
                    "Submission checks: NOT submittable - %s",
                    "; ".join(result.submission_failures) or "unknown reason",
                )
        else:
            self.log.warning(
                "Submission checks were not run for %s; submittability is UNVERIFIED",
                result.remote_alpha_id or label,
            )
        if result.is_one_sided_book:
            self.log.warning(
                "Alpha %s has a one-sided book (long=%s short=%s): its %s grade reflects "
                "directional concentration, not a market-neutral signal.",
                result.remote_alpha_id or label, result.long_count, result.short_count,
                result.grade or "unknown",
            )
        ratio = result.overfit_ratio
        if ratio is not None and ratio < OVERFIT_RATIO_THRESHOLD:
            self.log.warning(
                "Alpha %s degrades out of sample: test-year sharpe %.2f vs in-sample "
                "%.2f (ratio %.2f). The %s grade covers the full IS window, so treat "
                "it as possibly overfit.",
                result.remote_alpha_id or label, result.test_sharpe, result.sharpe,
                ratio, result.grade or "unknown",
            )
        if outcome.passed:
            self.log.info("PASSED")
        else:
            self.log.info("FAILED:\n%s", "\n".join(f"  {reason}" for reason in outcome.reasons))
        self._record(result, settings)
        return result

    def _poll(
        self,
        remote_simulation_id: str,
        simulation_row_id: int,
        label: str,
        *,
        max_wait: float | None = None,
    ) -> str:
        """Poll until the simulation finishes. Returns the remote alpha id.

        Cadence: the server's ``Retry-After`` when it sends one, otherwise
        ``poll_interval`` plus uniform jitter, always clamped into
        ``[MIN_POLL_DELAY, MAX_POLL_DELAY]`` so the loop can never spin tight.
        """
        runner = self.config.runner
        budget = float(runner.max_wait) if max_wait is None else float(max_wait)
        deadline = self._clock() + max(0.0, budget)
        last_bucket = -1
        logged_running = False

        while True:
            try:
                status = self.client.get_simulation_status(remote_simulation_id)
            except AuthError:
                self.store.update_simulation(
                    simulation_row_id,
                    status=SimulationStatus.AUTH_ERROR,
                    error="authentication failed while polling",
                    mark_completed=True,
                )
                raise
            except APIError as exc:
                self.store.update_simulation(
                    simulation_row_id,
                    status=SimulationStatus.REQUEST_ERROR,
                    error=str(exc),
                    mark_completed=True,
                )
                raise

            state = status.get("status")

            if state == SimulationStatus.COMPLETED:
                alpha_id = status.get("alpha_id")
                if not alpha_id:
                    raise SimulationFailedError(
                        "simulation reported completion without an alpha id",
                        expression=label,
                        simulation_id=remote_simulation_id,
                    )
                self.store.update_simulation(
                    simulation_row_id,
                    status=SimulationStatus.COMPLETED,
                    remote_alpha_id=str(alpha_id),
                    mark_completed=True,
                )
                return str(alpha_id)

            if state == SimulationStatus.FAILED:
                message = str(status.get("message") or "remote simulation failed")
                self.store.update_simulation(
                    simulation_row_id,
                    status=SimulationStatus.FAILED,
                    error=message,
                    mark_completed=True,
                )
                raise SimulationFailedError(
                    message, expression=label, simulation_id=remote_simulation_id
                )

            if not logged_running:
                self.store.update_simulation(simulation_row_id, status=SimulationStatus.RUNNING)
                self.log.info("Simulation %s running", remote_simulation_id)
                logged_running = True

            progress = status.get("progress")
            if isinstance(progress, (int, float)):
                bucket = int(float(progress) * 10)
                if bucket != last_bucket:
                    last_bucket = bucket
                    self.log.info(
                        "Simulation %s running (%d%%)", remote_simulation_id, min(100, bucket * 10)
                    )

            remaining = deadline - self._clock()
            if remaining <= 0:
                message = (
                    f"simulation {remote_simulation_id} did not finish within "
                    f"{budget:.0f}s (last progress={progress})"
                )
                self.store.update_simulation(
                    simulation_row_id,
                    status=SimulationStatus.TIMEOUT,
                    error=message,
                    mark_completed=True,
                )
                raise SimulationTimeoutError(
                    message, expression=label, simulation_id=remote_simulation_id
                )

            retry_after = status.get("retry_after")
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                delay = float(retry_after)
            else:
                delay = float(runner.poll_interval) + random.uniform(0, max(0.0, runner.poll_jitter))
            delay = max(MIN_POLL_DELAY, min(delay, MAX_POLL_DELAY, remaining))
            self._sleep(delay)

    # ------------------------------------------------------------------ #
    # Failure helpers
    # ------------------------------------------------------------------ #
    def _record(self, result: AlphaResult, settings: dict[str, Any]) -> None:
        """Append one simulation to the xlsx experiment ledger.

        Recording is best-effort: a ledger failure is logged and ignored, because
        losing a row must never discard a backtest that already succeeded.
        """
        if self.experiment_log is None:
            return
        try:
            self.experiment_log.record(result, settings)
        except Exception as exc:  # noqa: BLE001 - the ledger must never break a run
            self.log.warning(
                "could not record %s in the experiment ledger: %s", result.alpha_id, exc
            )

    def _retire_if_still_pending(self, simulation_row_id: int) -> None:
        """Retire an orphaned row that resume could not make progress on.

        ``run_spec`` resolves its target by dedup key. If settings normalization
        changed since the row was written, the key now points at a different alpha
        row, so this one is never driven to a terminal state and would be picked
        up again on every startup. Marking it SKIPPED records what happened and
        stops the churn without deleting any history.
        """
        row = self.store.get_simulation(simulation_row_id)
        if row is None or row.get("status") not in SimulationStatus.RESUMABLE:
            return

        self.log.warning(
            "Retiring simulation row %d (status=%s): its dedup key no longer "
            "resolves to this alpha, so resume cannot make progress on it. "
            "The equivalent alpha is tracked under its current key.",
            simulation_row_id, row.get("status"),
        )
        self.store.update_simulation(
            simulation_row_id,
            status=SimulationStatus.SKIPPED,
            error=(
                "superseded: settings normalization changed, so this row's dedup "
                "key no longer matches the alpha it was created for"
            ),
            mark_completed=True,
        )

    def _fail(
        self,
        spec: AlphaSpec,
        settings: dict[str, Any],
        key: str,
        simulation_row_id: int,
        status: str,
        error: str,
        *,
        remote_simulation_id: str | None = None,
        remote_alpha_id: str | None = None,
    ) -> AlphaResult:
        """Persist a non-completed outcome and return it as a result."""
        self.store.update_simulation(
            simulation_row_id,
            status=status,
            error=error,
            remote_simulation_id=remote_simulation_id,
            remote_alpha_id=remote_alpha_id,
            mark_completed=True,
        )
        result = AlphaResult(
            alpha_id=spec.name or spec.label,
            expression=spec.expression,
            dedup_key=key,
            status=status,
            simulation_id=remote_simulation_id,
            remote_alpha_id=remote_alpha_id,
            settings_json=_dumps(settings),
            created_at=utcnow_iso(),
            completed_at=utcnow_iso(),
            error=error,
            passed=False,
            reasons=[f"Status {status} != {SimulationStatus.COMPLETED} ({error})"],
        )
        self.store.save_result(simulation_row_id, result)
        self._record(result, settings)
        return result

    def _error_result(
        self,
        spec: AlphaSpec,
        exc: BaseException,
        *,
        status: str = SimulationStatus.REQUEST_ERROR,
    ) -> AlphaResult:
        """Build an in-memory failure result (used when no DB row exists yet)."""
        settings = self._effective_settings(spec)
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, SimulationError):
            status = SimulationStatus.FAILED
        return AlphaResult(
            alpha_id=spec.name or spec.label,
            expression=spec.expression,
            dedup_key=dedup_key(spec.expression, settings),
            status=status,
            settings_json=_dumps(settings),
            created_at=utcnow_iso(),
            completed_at=utcnow_iso(),
            error=message,
            passed=False,
            reasons=[f"Status {status} != {SimulationStatus.COMPLETED} ({message})"],
        )


def _dumps(payload: Any) -> str:
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"
