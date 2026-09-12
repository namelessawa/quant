#!/usr/bin/env python3
"""Search for an alpha whose BRAIN grade matches a target, e.g. AVERAGE.

Runs candidates in chunks, checks the grade BRAIN assigned after each chunk, and
stops as soon as one matches. Concurrency is capped at 3 simulations at a time.

A candidate counts as found only when it satisfies both halves of the acceptance
rule: the grade reaches ``--target-grade``, and all eight BRAIN submission checks
read PASS. The checks come from ``GET /alphas/{id}/check``, which every completed
simulation is put through — the plain alpha payload leaves ``SELF_CORRELATION``
PENDING forever, so the grade on its own is not evidence that an alpha can be
submitted.

The local database is consulted first, so an alpha already backtested under the
same expression+settings satisfies the search without spending any quota.

Example
-------
::

    python scripts/search_alpha.py --target-grade AVERAGE --concurrency 3
    python scripts/search_alpha.py --target-grade GOOD --max-attempts 120
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from worldquant import (  # noqa: E402
    AlphaGrade,
    AlphaResult,
    ResultStore,
    SimulationRunner,
    WorldQuantClient,
    WorldQuantError,
    build_ledger,
    describe_corr_verdicts,
    evaluate_acceptance,
    gate_specs,
    get_logger,
    load_alphas,
    load_config,
    open_registry,
    record_results,
    require_credentials,
    scope_hash,
    setup_logging,
    specs_from_expressions,
)
from worldquant.api import SUBMISSION_CHECKS, SimulationStatus, passed_check_count  # noqa: E402
from worldquant.config import ConfigError, apply_storage_overrides  # noqa: E402
from worldquant.generator import CombinatorialGenerator  # noqa: E402
from worldquant.models import AlphaSpec  # noqa: E402

EXIT_FOUND = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE_ERROR = 2

#: Price/volume fields. Kept for completeness but tried LAST: nine live rounds
#: showed plain pv signals cap out near fitness 0.83 (57 consecutive INFERIOR),
#: because decay and smoothing cut sharpe about as fast as they cut turnover.
POOL_FIELDS = ("close", "open", "high", "low", "volume", "returns", "vwap", "adv20")
#: Time-series operators confirmed present in ``GET /operators`` for this account
#: (66 accessible in total). ``ts_max`` / ``ts_min`` are **not** among them: using
#: one fails the simulation outright with "Attempted to use inaccessible or
#: unknown operator", which cost three round-24 candidates. Check ``/operators``
#: before adding anything here.
POOL_OPERATORS = ("ts_delta", "ts_mean", "ts_std_dev", "ts_rank", "ts_sum",
                  "ts_zscore", "ts_arg_max")
POOL_WINDOWS = (5, 10, 20, 60)

#: Analyst-estimate numerators paired with the correct denominator: market cap
#: for aggregate figures, price for per-share ones. All verified present in
#: USA / TOP3000 / delay 1 with coverage 0.78-0.95. Earnings-yield ratios built
#: from these reached fitness 1.09-1.66, i.e. AVERAGE and GOOD.
YIELD_NUMERATORS: tuple[tuple[str, str], ...] = (
    ("anl4_ebit_value", "cap"),
    ("anl4_ebitda_value", "cap"),
    ("anl4_adjusted_netincome_ft", "cap"),
    ("est_eps", "close"),
    ("anl4_tbve_ft", "close"),
)

#: The recipe that reached EXCELLENT (fitness 2.30, sharpe 2.35, drawdown 4.0%,
#: balanced book, every check passing): blend an aggregate yield with an analyst
#: consensus yield, take its deviation from a 120-day mean, smooth, winsorize to
#: cap the drawdown, then standardize within **subindustry**.
#:
#: Two ablations earned their place here. ``subindustry`` beat ``industry`` on the
#: same field (fitness 0.82 -> 1.00), and dropping the ``trade_when`` regime
#: filter raised sharpe (1.32 -> 1.41) — the filter cost more signal than the
#: volatility it avoided.
_BLEND_TEMPLATE = (
    "ey = ts_backfill(anl4_ebit_value, 40) / cap; "
    "np = ts_backfill({consensus}, 40) / cap; "
    "s = ey + np; "
    "group_zscore(winsorize(ts_mean(s - ts_mean(s, 120), 20), std=3.0), subindustry)"
)

#: Single-field version of the same structure.
_STACK_TEMPLATE = (
    "ey = ts_backfill({num}, 40) / {den}; "
    "group_zscore(winsorize(ts_mean(ey - ts_mean(ey, 120), 20), std=3.0), subindustry)"
)

#: Plain yield ranks. Demoted to last: they grade well only by going long-only
#: (``shortCount == 0``) under ``neutralization=NONE``, which inflates fitness
#: through directional concentration rather than cross-sectional signal.
_YIELD_TEMPLATE = "rank(ts_backfill({num}, 40) / {den})"

#: Analyst consensus fields from the ``..._estimates_advanced_af_nd_*`` family,
#: all verified present for USA / TOP3000 / delay 1 with coverage 0.60-0.85.
CONSENSUS_FIELDS: tuple[str, ...] = (
    "anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean",
    "anl4_fs_detail_estimates_advanced_af_nd_ebitda_mean",
    "anl4_fs_detail_estimates_advanced_af_nd_cfo_mean",
    "anl4_fs_detail_estimates_advanced_af_nd_fcf_mean",
    "anl4_fs_detail_estimates_advanced_af_nd_grossincome_mean",
)


def _yield_seeds() -> list[str]:
    """Blends first, then single-field stacks, then the plain yields."""
    seeds = [_BLEND_TEMPLATE.format(consensus=field) for field in CONSENSUS_FIELDS]
    seeds += [
        _STACK_TEMPLATE.format(num=numerator, den=denominator)
        for numerator, denominator in YIELD_NUMERATORS
    ]
    seeds += [
        _YIELD_TEMPLATE.format(num=numerator, den=denominator)
        for numerator, denominator in YIELD_NUMERATORS
    ]
    return seeds


#: Tried first, highest prior first.
SEED_EXPRESSIONS: tuple[str, ...] = tuple(_yield_seeds())


def build_pool(negate_first: bool = True) -> list[str]:
    """Assemble candidate expressions, most promising first.

    Analyst-estimate earnings yields lead: they are the only family observed to
    reach AVERAGE/GOOD fitness, with the winsorize + regime-filter stack ahead of
    the plain yields because it also keeps drawdown low enough to pass the
    submission checks.

    The price/volume cross product is appended last. It is a known dead end for
    grading — 57 consecutive INFERIOR results, capped near fitness 0.83 — but it
    is cheap to keep as a tail for anyone who wants to re-explore it. Within that
    tail ``negate_first`` controls the sign order, since a strongly negative
    sharpe is as informative as a positive one.
    """
    expressions: list[str] = list(SEED_EXPRESSIONS)

    for sign in (("-", "") if negate_first else ("", "-")):
        generator = CombinatorialGenerator(
            operators=POOL_OPERATORS,
            fields=POOL_FIELDS,
            windows=POOL_WINDOWS,
            wrapper="rank",
            template=sign + "rank({operator}({field}, {window}))",
        )
        expressions.extend(generator.generate())

    # De-duplicate while preserving priority order.
    seen: set[str] = set()
    unique: list[str] = []
    for expression in expressions:
        if expression in seen:
            continue
        seen.add(expression)
        unique.append(expression)
    return unique


def parse_setting_overrides(pairs: list[str]) -> dict[str, object]:
    """Turn ``--setting KEY=VALUE`` pairs into a typed settings dict.

    Numbers and booleans are coerced from their text form so ``--setting decay=8``
    reaches BRAIN as an integer rather than the string ``"8"``.
    """
    overrides: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(
                f"--setting expects KEY=VALUE, got {pair!r} (e.g. --setting decay=8)"
            )
        key, _, raw_value = pair.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if not key:
            raise ConfigError(f"--setting {pair!r} has an empty key")

        lowered = value.lower()
        if lowered in {"true", "false"}:
            overrides[key] = lowered == "true"
            continue
        try:
            overrides[key] = int(value)
            continue
        except ValueError:
            pass
        try:
            overrides[key] = float(value)
            continue
        except ValueError:
            pass
        overrides[key] = value
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="search_alpha.py",
        description="Simulate candidates until one receives the target BRAIN grade.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--target-grade", default=AlphaGrade.AVERAGE,
        help="grade to search for (observed values: INFERIOR, AVERAGE, GOOD)",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=60,
        help="stop after simulating this many candidates, even without a hit",
    )
    parser.add_argument("--concurrency", type=int, default=3, help="parallel simulations (1..3)")
    parser.add_argument("--input", "-i", type=str, help="take candidates from a file instead of the built-in pool")
    parser.add_argument("--config", "-c", type=str, help="path to a YAML/JSON config file")
    parser.add_argument("--credentials", type=str, help="path to a JSON credentials file")
    parser.add_argument("--db", type=str, help="override the SQLite database path")
    parser.add_argument("--log-file", type=str, help="override the log file path")
    parser.add_argument("--log-level", type=str, help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("--poll-interval", type=float, help="seconds between status polls")
    parser.add_argument("--max-wait", type=float, help="seconds to wait per simulation")
    parser.add_argument(
        "--min-request-interval", type=float,
        help="global minimum seconds between any two HTTP requests; raise this if "
             "BRAIN answers with HTTP 429",
    )
    parser.add_argument(
        "--setting", action="append", default=[], metavar="KEY=VALUE",
        help="override one backtest setting, repeatable (e.g. --setting decay=8 "
             "--setting neutralization=SUBINDUSTRY). Strongly affects turnover and "
             "fitness, which drive BRAIN's grade.",
    )
    parser.add_argument(
        "--no-yearly", action="store_true", help="skip the yearly-stats call (faster per alpha)"
    )
    parser.add_argument(
        "--include-existing", action="store_true",
        help="also accept a matching grade already stored from an earlier run",
    )
    parser.add_argument(
        "--no-registry", action="store_true",
        help="disable the factor registry (memory/duplicate/novelty gate) for this run",
    )
    return parser


def grade_meets_target(grade: str | None, target: str) -> bool:
    """True when ``grade`` is the target or better.

    The target is a floor, not an exact match: a live run searching for GOOD
    once produced EXCELLENT and, because the comparison was ``grade == target``,
    sailed straight past it and reported "no alpha graded GOOD". Unknown grades
    never satisfy the check, and neither does an unrecognized target, so a typo
    in ``--target-grade`` cannot silently match everything.
    """
    if not grade:
        return False
    target_rank = AlphaGrade.rank(target)
    if target_rank < 0:
        return False
    return AlphaGrade.rank(grade) >= target_rank


def is_acceptable_hit(result: AlphaResult, target: str) -> bool:
    """True only when the grade clears the target **and** all eight checks PASS.

    Grade and submittability are independent verdicts: the grade follows fitness
    alone, so GOOD-graded alphas carrying two or three failing checks have been
    observed live. An alpha that cannot be submitted is not a find, and an alpha
    whose checks were never resolved is not a find either.
    """
    return grade_meets_target(result.grade, target) and result.is_submittable


def describe_submission(result: AlphaResult) -> str:
    """One-line submission verdict.

    Keeps "never checked" visibly distinct from "checked and failed". The first
    means the ``/check`` endpoint returned nothing — worth retrying — while the
    second names what is blocking the alpha, which is what decides whether to
    revise the expression or abandon it.
    """
    checks = result.submission_checks
    if not checks:
        return "UNVERIFIED - GET /alphas/{id}/check returned no results"
    verdict = f"{passed_check_count(checks)}/{len(SUBMISSION_CHECKS)} checks PASS"
    if result.self_correlation is not None:
        verdict += f", self-correlation={result.self_correlation:.4f} (limit 0.7)"
    failures = result.submission_failures
    if failures:
        verdict += " | FAILING: " + "; ".join(failures)
    return verdict


#: Metrics shown for each of the ``train`` / ``test`` blocks.
_STAGE_METRICS: tuple[str, ...] = ("sharpe", "fitness", "returns", "turnover", "drawdown")

#: Which of those are fractions and therefore read better as percentages.
_STAGE_RATIOS: frozenset[str] = frozenset({"returns", "turnover", "drawdown"})


def _stage_line(stage: Any) -> str:
    """Render one ``train`` / ``test`` block on a single line."""
    if not isinstance(stage, dict) or not stage:
        return "n/a"
    parts: list[str] = []
    for name in _STAGE_METRICS:
        value = stage.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parts.append(
            f"{name}={value * 100:.1f}%" if name in _STAGE_RATIOS else f"{name}={value:.2f}"
        )
    return " ".join(parts) or "n/a"


def report_hit(result: AlphaResult, log, *, attempts: int) -> None:
    log.info("=" * 64)
    log.info("TARGET GRADE %s FOUND after %d attempt(s)", result.grade, attempts)
    log.info("  expression : %s", result.expression)
    log.info("  alpha id   : %s", result.remote_alpha_id)
    log.info("  %s", result.metrics_line())
    log.info("  long/short : %s / %s", result.long_count, result.short_count)
    if isinstance(result.test_stats, dict) and result.test_stats:
        log.info("  train      : %s", _stage_line(result.train_stats))
        log.info("  test (P1Y) : %s", _stage_line(result.test_stats))
    log.info("  submission : %s", describe_submission(result))
    summary = result.yearly_summary
    if not summary.is_empty:
        log.info(
            "  yearly     : %d/%d positive, worst=%s, std=%s",
            summary.positive_years, summary.total_years,
            f"{summary.worst_year_sharpe:.2f}" if summary.worst_year_sharpe is not None else "n/a",
            f"{summary.yearly_sharpe_std:.2f}" if summary.yearly_sharpe_std is not None else "n/a",
        )
    log.info("  url        : https://platform.worldquantbrain.com/alpha/%s", result.remote_alpha_id)
    if result.is_one_sided_book:
        log.warning(
            "  CAVEAT     : one-sided book (long=%s short=%s). The %s grade comes from "
            "directional concentration, not a market-neutral signal, so this is not a "
            "usable alpha despite the grade.",
            result.long_count, result.short_count, result.grade,
        )
    log.info("=" * 64)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = str(args.target_grade).strip().upper()

    provisional = setup_logging(None, "INFO")

    overrides: dict[str, object] = {}
    if args.concurrency is not None:
        overrides["concurrency"] = args.concurrency
    if args.poll_interval is not None:
        overrides["poll_interval"] = args.poll_interval
    if args.max_wait is not None:
        overrides["max_wait"] = args.max_wait
    if args.min_request_interval is not None:
        overrides["min_request_interval"] = args.min_request_interval
    if args.no_yearly:
        overrides["fetch_yearly_stats"] = False

    try:
        setting_overrides = parse_setting_overrides(args.setting)
        if setting_overrides:
            overrides["settings"] = setting_overrides
        config = load_config(args.config, overrides=overrides, credentials_path=args.credentials)
        storage_overrides = {
            key: value
            for key, value in (("db_path", args.db), ("log_file", args.log_file))
            if value
        }
        if storage_overrides:
            config = apply_storage_overrides(config, **storage_overrides)
        setup_logging(config.storage.log_file, args.log_level or config.storage.log_level)
        credentials = require_credentials(config)
    except ConfigError as exc:
        provisional.error("Configuration error: %s", exc)
        return EXIT_USAGE_ERROR

    log = get_logger("search")

    if args.input:
        try:
            specs = load_alphas(args.input, default_settings=config.settings)
        except (ConfigError, WorldQuantError) as exc:
            log.error("%s", exc)
            return EXIT_USAGE_ERROR
    else:
        pool = build_pool()
        if args.max_attempts < len(pool):
            pool = pool[: args.max_attempts]
        specs = specs_from_expressions(pool, settings=config.settings)

    if args.max_attempts >= 0:
        specs = specs[: args.max_attempts]
    if not specs:
        if args.max_attempts <= 0:
            log.error("--max-attempts is %d, so there is nothing to simulate", args.max_attempts)
        else:
            log.error("no candidates to try: the source produced an empty list")
        return EXIT_USAGE_ERROR

    chunk_size = max(1, min(config.runner.concurrency, len(specs)))
    log.info(
        "Searching for grade=%s | %d candidate(s) before dedup, cap=%d attempts",
        target, len(specs), args.max_attempts,
    )
    log.info(
        "Default settings: region=%s universe=%s delay=%s decay=%s neutralization=%s truncation=%s",
        config.settings.get("region"), config.settings.get("universe"),
        config.settings.get("delay"), config.settings.get("decay"),
        config.settings.get("neutralization"), config.settings.get("truncation"),
    )
    if args.input:
        log.info(
            "Note: per-row settings columns in %s override these defaults for each "
            "candidate; the ledger records what each one actually used.",
            args.input,
        )

    attempted = 0
    grade_tally: dict[str, int] = {}

    registry_cm = (
        nullcontext()
        if args.no_registry
        else (open_registry(config, log=log) or nullcontext())
    )
    with WorldQuantClient(
        credentials,
        base_url=config.base_url,
        retry=config.retry,
        min_request_interval=config.runner.min_request_interval,
        yearly_stats_path=config.yearly_stats_path,
    ) as client, ResultStore(config.storage.db_path) as store, registry_cm as registry:
        if args.include_existing:
            # find_by_grade matches exactly, so scan and compare by rank: a
            # stored EXCELLENT must satisfy a search for GOOD.
            graded = [
                result
                for result in store.all_results(statuses=[SimulationStatus.COMPLETED])
                if grade_meets_target(result.grade, target)
            ]
            # An alpha that has been marked excluded (e.g.
            # SELF_CORRELATION_BLOCKED after a one-by-one screening) is
            # permanently out of the usable pool — do not let --include-existing
            # surface it as a candidate.
            graded = [result for result in graded if not result.is_excluded]
            # The grade is only half the acceptance rule. Rows stored before the
            # check endpoint was wired up carry no checks at all, and an
            # unchecked alpha is not a verified one.
            candidates = [result for result in graded if result.is_submittable]
            if len(candidates) < len(graded):
                log.info(
                    "%d stored alpha(s) grade %s or better, but %d have no recorded "
                    "8/8 submission check, so they are not reported as hits",
                    len(graded), target, len(graded) - len(candidates),
                )
            if candidates:
                # Rank first, then fitness: several alphas can share the top
                # grade, and the strongest one is the useful answer.
                best = max(
                    candidates,
                    key=lambda r: (AlphaGrade.rank(r.grade), r.fitness or float("-inf")),
                )
                log.info(
                    "Found %d verified alpha(s) graded %s or better; best is %s",
                    len(candidates), target, best.grade,
                )
                report_hit(best, log, attempts=0)
                return EXIT_FOUND

        # The Registry research gate runs FIRST, before the ResultStore
        # execution-dedup: every candidate must be visible to research memory
        # (duplicate attempts, explicit ablation sweeps), and a scope-hash hit
        # in the local store must not hide a candidate from the gate. Blocked
        # candidates get a Registry row (status/reason/novelty) but never a
        # fake ResultStore execution.
        if specs:
            gate_result = gate_specs(registry, specs, log=log)
            specs = gate_result.kept
            if registry is not None and not specs:
                log.info(
                    "Every candidate is a known duplicate or a saturated "
                    "low-novelty variant; nothing new to simulate."
                )
                return EXIT_NOT_FOUND

        # ResultStore dedup comes SECOND and answers a narrower question —
        # "does this exact information set already have a real simulation
        # execution?" — rather than "is this a worthwhile research attempt?".
        # "Identical" means the same expression over the same information set:
        # region, universe and delay decide what is predicted and with what
        # latency, so the same signal in TOP1000 is a different alpha. The
        # remaining settings are portfolio construction — one expression run
        # under two truncation values once produced effectively identical
        # results (both fitness 2.07) and wasted quota, so those still do not
        # make a new alpha.
        already_run = store.simulated_scope_hashes()
        if already_run:
            before = len(specs)
            specs = [
                spec for spec in specs
                if scope_hash(spec.expression, spec.settings) not in already_run
            ]
            dropped = before - len(specs)
            if dropped:
                log.info(
                    "Skipped %d candidate(s) already simulated in the same "
                    "region/universe/delay; %d remain",
                    dropped, len(specs),
                )
            if not specs:
                log.info(
                    "Every candidate has already been simulated in its scope. "
                    "Nothing to do (use run_backtest.py --force to re-run "
                    "deliberately)."
                )
                return EXIT_NOT_FOUND
            specs = specs[: args.max_attempts]

        chunk_size = max(1, min(config.runner.concurrency, len(specs)))
        log.info("Running %d candidate(s), %d at a time", len(specs), chunk_size)

        ledger = build_ledger(config, client, logger=log)
        log.info("Experiment ledger -> %s", config.storage.ledger_path)
        runner = SimulationRunner(client, store, config, experiment_log=ledger)

        try:
            client.ensure_authenticated()

            for start in range(0, len(specs), chunk_size):
                if runner.halted:
                    log.error("Stopping search: %s", runner.halt_reason)
                    return EXIT_NOT_FOUND

                chunk: list[AlphaSpec] = specs[start:start + chunk_size]
                attempted += len(chunk)
                log.info(
                    "--- attempt %d-%d of <=%d ---",
                    start + 1, attempted, args.max_attempts,
                )
                results = runner.run_batch(chunk, force=False)

                for result in results:
                    grade = (result.grade or "").upper()
                    if result.status == SimulationStatus.COMPLETED:
                        grade_tally[grade or "NONE"] = grade_tally.get(grade or "NONE", 0) + 1
                        log.info(
                            "  %-10s sharpe=%-7s %s",
                            grade or "n/a",
                            f"{result.sharpe:.2f}" if result.sharpe is not None else "n/a",
                            result.expression[:58],
                        )
                    else:
                        log.warning("  %-10s %s (%s)", result.status, result.expression[:50],
                                    (result.error or "")[:120])

                    # Fold the attempt into factor memory FIRST: the research
                    # corr verdict is part of acceptance, not bookkeeping that
                    # happens after the decision. Never blocks the search loop.
                    outcome: dict[str, Any] | None = None
                    if registry is not None:
                        outcome = record_results(registry, [result])[0][1]

                    if not grade_meets_target(grade, target):
                        continue

                    corr_cfg = (
                        registry.config.correlation if registry is not None else None
                    )
                    # With the registry disabled (--no-registry) there is no
                    # research verdict at all, so fall back to the BRAIN-only
                    # rule. With it enabled, UNKNOWN rejects unless the operator
                    # explicitly sets allow_submit_without_corr.
                    allow_unknown = (
                        registry is None
                        or bool(getattr(corr_cfg, "allow_submit_without_corr", False))
                    )
                    acceptance = evaluate_acceptance(
                        result,
                        outcome,
                        grade_ok=True,
                        allow_unknown=allow_unknown,
                        corr_config=corr_cfg,
                    )
                    if acceptance.accepted:
                        report_hit(result, log, attempts=attempted)
                        log.info("Grade tally so far: %s", grade_tally)
                        return EXIT_FOUND

                    # Grade clears the target but the alpha is not a find. Say
                    # exactly which of the two independent gates rejected it so
                    # a value like 0.68 (PASS vs BRAIN 0.70, FAIL vs research
                    # 0.65) never looks like contradictory program logic.
                    if not result.is_submittable:
                        log.warning(
                            "  %s graded %s but is NOT submittable - %s; abandoning it "
                            "and continuing the search",
                            result.remote_alpha_id or result.alpha_id, grade,
                            describe_submission(result),
                        )
                    else:
                        log.warning(
                            "  %s graded %s passes BRAIN submission checks but is "
                            "NOT accepted by the local research gate; abandoning "
                            "it and continuing the search",
                            result.remote_alpha_id or result.alpha_id, grade,
                        )
                        log.warning(
                            "  %s",
                            describe_corr_verdicts(result, outcome, corr_config=corr_cfg),
                        )
                        if acceptance.research_corr_status == "UNKNOWN":
                            log.warning(
                                "  GOOD alpha found but correlation unresolved; "
                                "not accepted."
                            )
                        log.warning(
                            "Rejected by local diversity gate despite official "
                            "BRAIN submission pass."
                        )

                if attempted >= args.max_attempts:
                    log.info("Reached the --max-attempts cap of %d", args.max_attempts)
                    break

        except KeyboardInterrupt:
            log.warning(
                "Interrupted. Progress is saved in %s; re-run to continue (completed "
                "candidates are skipped automatically).",
                config.storage.db_path,
            )
            return EXIT_NOT_FOUND
        except WorldQuantError as exc:
            log.error("Search failed: %s", exc)
            return EXIT_NOT_FOUND

        log.info(
            "No alpha graded %s or better with 8/8 submission checks PASS after %d "
            "attempt(s). Tally: %s",
            target, attempted, grade_tally,
        )
        log.info("Stored grades overall: %s", store.grade_counts() or "{}")

    return EXIT_NOT_FOUND


if __name__ == "__main__":
    sys.exit(main())
