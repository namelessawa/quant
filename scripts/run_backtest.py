#!/usr/bin/env python3
"""Run WorldQuant BRAIN backtests for a batch of alphas.

Examples
--------
Single alpha::

    python scripts/run_backtest.py --expression "rank(ts_delta(close, 5))"

Batch from a file::

    python scripts/run_backtest.py --input data/alphas.csv --config config.yaml

Finish work left behind by an interrupted run::

    python scripts/run_backtest.py --resume-only

Regenerate CSV exports and the leaderboard without touching the network::

    python scripts/run_backtest.py --export-only --top 20
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path

# Allow running from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from worldquant import (  # noqa: E402
    AlphaResult,
    AppConfig,
    FieldCatalog,
    ResultStore,
    SimulationRunner,
    WorldQuantClient,
    WorldQuantError,
    build_ledger,
    dedup_key,
    format_leaderboard,
    get_logger,
    latest_per_alpha,
    load_alphas,
    load_config,
    require_credentials,
    setup_logging,
    specs_from_expressions,
    summarize_yearly,
    validate_filter_config,
    write_ledger_from_results,
)
from worldquant.api import AlphaGrade, SimulationStatus  # noqa: E402
from worldquant.config import ConfigError, apply_storage_overrides  # noqa: E402
from worldquant.registry import (  # noqa: E402
    gate_specs,
    open_registry,
    record_results,
)

EXIT_OK = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_USAGE_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_backtest.py",
        description="Submit, poll, screen and store WorldQuant BRAIN alpha backtests.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    source = parser.add_argument_group("alpha source")
    source.add_argument("--input", "-i", type=str, help="path to a .csv/.txt/.json alpha list")
    source.add_argument(
        "--expression", "-e", action="append", default=[],
        help="run a single expression directly; repeatable",
    )

    parser.add_argument("--config", "-c", type=str, help="path to a YAML/JSON config file")
    parser.add_argument(
        "--credentials", type=str,
        help='path to a JSON credentials file shaped {"email": "...", "password": "..."} '
             "(default: credentials.json auto-discovered in the project root)",
    )
    parser.add_argument("--db", type=str, help="override the SQLite database path")
    parser.add_argument("--log-level", type=str, help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("--log-file", type=str, help="override the log file path")

    behaviour = parser.add_argument_group("run behaviour")
    behaviour.add_argument(
        "--force", action="store_true",
        help="re-run alphas that already have a completed result",
    )
    behaviour.add_argument("--limit", type=int, help="run at most N alphas")
    behaviour.add_argument("--poll-interval", type=float, help="seconds between status polls")
    behaviour.add_argument("--poll-jitter", type=float, help="random seconds added to each poll")
    behaviour.add_argument("--max-wait", type=float, help="seconds to wait per simulation")
    behaviour.add_argument("--concurrency", type=int, help="parallel simulations (clamped to 1..3)")
    behaviour.add_argument(
        "--min-request-interval", type=float,
        help="global minimum seconds between any two HTTP requests",
    )
    behaviour.add_argument(
        "--no-yearly", action="store_true", help="skip the yearly-stats enrichment call"
    )
    behaviour.add_argument(
        "--no-registry", action="store_true",
        help="disable the Factor Registry research-memory gate and recording "
             "(enabled by default); --force also bypasses the registry gate",
    )
    behaviour.add_argument(
        "--ablation-group", metavar="ID",
        help="tag every loaded candidate as an explicit ablation-sweep variant "
             "sharing group ID: same-signal variants (different "
             "decay/truncation/neutralization) are allowed to simulate, while "
             "exact experiment duplicates remain blocked unless --force. "
             "Per-row provenance comes from CSV/JSON columns "
             "(source/ablation_group_id/changed_parameters/parent_experiment_id)",
    )

    mode = parser.add_argument_group("modes")
    mode.add_argument(
        "--resume-only", action="store_true",
        help="only finish simulations left incomplete by a previous run",
    )
    mode.add_argument(
        "--export-only", action="store_true",
        help="regenerate CSVs and the leaderboard from the database; no network access",
    )
    mode.add_argument(
        "--rebuild-ledger", action="store_true",
        help="with --export-only: rewrite the xlsx experiment ledger from the database",
    )
    mode.add_argument(
        "--refresh-yearly", action="store_true",
        help="re-fetch the per-year recordset for stored alphas that have none; "
             "no simulation is submitted",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--top", type=int, default=20, help="leaderboard size")
    output.add_argument("--data-dir", type=str, help="override the CSV export directory")

    return parser


def collect_specs(args: argparse.Namespace, config: AppConfig) -> list:
    """Merge ``--expression`` and ``--input`` into one de-duplicated spec list."""
    specs = []
    if args.expression:
        specs.extend(specs_from_expressions(args.expression, settings=config.settings))
    if args.input:
        specs.extend(load_alphas(args.input, default_settings=config.settings))

    seen: set[str] = set()
    unique = []
    for spec in specs:
        key = dedup_key(spec.expression, spec.settings)
        if key in seen:
            continue
        seen.add(key)
        unique.append(spec)
    return unique


def summarize(results: list[AlphaResult], log) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    log.info("-" * 60)
    log.info(
        "Run summary: %d alpha(s) | passed=%d failed=%d | %s",
        len(results), passed, failed,
        " ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "none",
    )
    return counts


def export_and_report(
    store: ResultStore,
    config: AppConfig,
    results: list[AlphaResult],
    log,
    *,
    top: int,
    data_dir: Path | None = None,
) -> dict[str, Path]:
    directory = data_dir or config.storage.data_dir
    paths = store.export_all(directory)
    for label, path in paths.items():
        log.info("Wrote %s -> %s", label, path)

    # Rank from the database, not just this run, so the leaderboard reflects
    # every completed alpha ever stored. Collapse repeats first: --force and
    # resumed timeouts leave several rows per alpha, and only the newest one
    # carries the current parsing of yearly stats.
    all_completed = latest_per_alpha(
        store.all_results(statuses=[SimulationStatus.COMPLETED])
    )
    leaderboard = format_leaderboard(all_completed, top=top)
    print()
    print(leaderboard)
    if all_completed:
        log.info("Leaderboard covers %d completed alpha(s)", len(all_completed))
    return paths


def run_export_only(config: AppConfig, args: argparse.Namespace, log) -> int:
    with ResultStore(config.storage.db_path) as store:
        stored = store.all_results()
        if not stored:
            log.warning("Database %s holds no simulations yet", config.storage.db_path)
        log.info("Stored simulations by status: %s", store.status_counts() or "{}")
        export_and_report(store, config, stored, log, top=args.top,
                          data_dir=Path(args.data_dir) if args.data_dir else None)

        if args.rebuild_ledger:
            ledger_path = config.storage.ledger_path
            # Rewrite rather than append: normal runs already add one row per
            # simulation, so appending here would duplicate the whole history on
            # every export.
            if ledger_path.exists():
                ledger_path.unlink()
            catalog = FieldCatalog(config.storage.field_catalog_path)
            written = write_ledger_from_results(ledger_path, stored, catalog=catalog, logger=log)
            log.info(
                "Rebuilt experiment ledger %s from %d stored simulation(s)", ledger_path, written
            )
            if not catalog.size:
                log.info(
                    "Dataset column is empty: the field catalog cache %s has no entries yet. "
                    "It fills in automatically during a live run.",
                    config.storage.field_catalog_path,
                )
    return EXIT_OK


def run_refresh_yearly(config: AppConfig, args: argparse.Namespace, credentials, log) -> int:
    """Re-fetch yearly stats for stored alphas that have none.

    A stage-label mismatch once made ``parse_yearly_stats`` drop every row for
    alphas simulated with a ``testPeriod``, so most stored results have no yearly
    data even though the endpoint still serves it. Re-simulating is not an option,
    but the recordset can simply be fetched again.
    """
    with WorldQuantClient(
        credentials,
        base_url=config.base_url,
        retry=config.retry,
        min_request_interval=config.runner.min_request_interval,
        yearly_stats_path=config.yearly_stats_path,
    ) as client, ResultStore(config.storage.db_path) as store:
        missing: dict[str, AlphaResult] = {}
        for result in store.all_results(statuses=[SimulationStatus.COMPLETED]):
            alpha_id = result.remote_alpha_id
            if alpha_id and not result.yearly_stats and alpha_id not in missing:
                missing[alpha_id] = result

        if not missing:
            log.info("Every stored alpha already has yearly stats; nothing to refresh")
            return EXIT_OK

        # Best grades first: --limit should spend its requests where the yearly
        # stability rules actually decide something.
        ordered = sorted(
            missing.values(),
            key=lambda r: -(AlphaGrade.rank(r.grade) if r.grade else -1),
        )
        if args.limit is not None:
            ordered = ordered[: args.limit]

        log.info(
            "Refreshing yearly stats for %d of %d stored alpha(s) that have none "
            "(best grades first, no simulation submitted)",
            len(ordered), len(missing),
        )

        try:
            client.ensure_authenticated()
        except WorldQuantError as exc:
            log.error("Cannot log in: %s", exc)
            return EXIT_RUNTIME_FAILURE

        filled = 0
        still_empty: list[str] = []
        for index, result in enumerate(ordered, start=1):
            alpha_id = result.remote_alpha_id
            try:
                stats = client.fetch_yearly_stats(alpha_id)
            except WorldQuantError as exc:
                log.warning("[%d/%d] %s: %s", index, len(ordered), alpha_id, exc)
                still_empty.append(alpha_id)
                continue

            if stats and store.record_yearly_stats(alpha_id, stats):
                filled += 1
                summary = summarize_yearly(stats)
                log.info(
                    "[%d/%d] %-10s %-11s %d year(s), %d positive, worst=%s",
                    index, len(ordered), alpha_id, result.grade or "n/a",
                    summary.total_years, summary.positive_years,
                    f"{summary.worst_year_sharpe:.2f}"
                    if summary.worst_year_sharpe is not None else "n/a",
                )
            else:
                still_empty.append(alpha_id)
                log.warning(
                    "[%d/%d] %-10s still has no yearly stats", index, len(ordered), alpha_id
                )

        log.info(
            "Yearly stats refreshed for %d alpha(s); %d still empty", filled, len(still_empty)
        )
        if filled:
            log.info(
                "Rebuild the ledger to pick them up: "
                "python scripts/run_backtest.py --export-only --rebuild-ledger"
            )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Logging must be configured before load_config so config warnings are visible.
    provisional = setup_logging(None, "INFO")
    overrides: dict[str, object] = {}
    for key in ("poll_interval", "poll_jitter", "max_wait", "concurrency",
                "min_request_interval"):
        value = getattr(args, key, None)
        if value is not None:
            overrides[key] = value
    if args.no_yearly:
        overrides["fetch_yearly_stats"] = False

    try:
        config = load_config(args.config, overrides=overrides, credentials_path=args.credentials)

        storage_overrides = {
            key: value
            for key, value in (
                ("db_path", args.db),
                ("log_file", args.log_file),
                ("data_dir", args.data_dir),
            )
            if value
        }
        if storage_overrides:
            config = apply_storage_overrides(config, **storage_overrides)

        validate_filter_config(config.filters)
        setup_logging(config.storage.log_file, args.log_level or config.storage.log_level)
    except ConfigError as exc:
        provisional.error("Configuration error: %s", exc)
        return EXIT_USAGE_ERROR
    except WorldQuantError as exc:
        provisional.error("%s", exc)
        return EXIT_USAGE_ERROR

    log = get_logger("cli")

    if args.export_only:
        return run_export_only(config, args, log)

    if not args.resume_only and not args.refresh_yearly:
        try:
            specs = collect_specs(args, config)
        except (ConfigError, WorldQuantError) as exc:
            log.error("%s", exc)
            return EXIT_USAGE_ERROR
        if getattr(args, "ablation_group", None):
            tagged = 0
            for spec in specs:
                if getattr(spec, "source", None):
                    continue
                spec.source = "ablation"
                spec.ablation_group_id = args.ablation_group
                tagged += 1
            log.info(
                "Tagged %d candidate(s) as ablation sweep %r",
                tagged, args.ablation_group,
            )
        if not specs:
            log.error(
                "no alphas to run: pass --input FILE, --expression EXPR, or --resume-only"
            )
            return EXIT_USAGE_ERROR
    else:
        specs = []

    try:
        credentials = require_credentials(config)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_USAGE_ERROR

    if args.refresh_yearly:
        return run_refresh_yearly(config, args, credentials, log)

    exit_code = EXIT_OK
    results: list[AlphaResult] = []

    # Research memory gate. Enabled by default; --no-registry restores the
    # pre-Registry execution path exactly. Opening does not require BRAIN auth.
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
    ) as client, ResultStore(config.storage.db_path) as store, \
            registry_cm as registry:
        runner = SimulationRunner(
            client, store, config, experiment_log=build_ledger(config, client, logger=log)
        )

        try:
            client.ensure_authenticated()

            if args.resume_only:
                results.extend(runner.resume_incomplete())
            else:
                # Drain leftovers from an earlier interrupted run first. Doing
                # this after the batch would retry whatever just failed in this
                # same run, reporting one deterministic error twice.
                results.extend(runner.resume_incomplete())
                if specs:
                    # Registry gate FIRST (research identity for every attempt,
                    # including blocked ones), ResultStore execution dedup
                    # second (inside the runner). --force bypasses both; the
                    # registry never writes fake ResultStore rows for blocks.
                    gate_result = gate_specs(
                        registry, specs, force=args.force, log=log
                    )
                    if registry is not None and gate_result.blocked:
                        log.info(
                            "Registry gate blocked %d/%d candidate(s) — no "
                            "simulation submitted for those; %d remain",
                            gate_result.blocked, len(specs), len(gate_result.kept),
                        )
                    if gate_result.kept:
                        results.extend(
                            runner.run_batch(
                                gate_result.kept,
                                force=args.force,
                                limit=args.limit,
                            )
                        )
                    elif registry is not None:
                        log.info(
                            "Every candidate was blocked by the Registry gate; "
                            "nothing to simulate."
                        )

            # resume_incomplete and run_batch can both report the same alpha:
            # resume finishes it, then the batch skips it as already complete.
            # Collapse so the summary counts alphas, not code paths.
            results = latest_per_alpha(results)

            # Fold every real execution (including resumed ones) into research
            # memory: metrics, lifecycle status and the research corr verdict.
            # Never raises into the run.
            record_results(registry, results)

            summarize(results, log)
            export_and_report(
                store, config, results, log, top=args.top,
                data_dir=Path(args.data_dir) if args.data_dir else None,
            )

            if runner.halted:
                log.error("Batch halted early: %s", runner.halt_reason)
                exit_code = EXIT_RUNTIME_FAILURE
            elif any(r.status == SimulationStatus.AUTH_ERROR for r in results):
                exit_code = EXIT_RUNTIME_FAILURE
            elif results and not any(r.status == SimulationStatus.COMPLETED for r in results):
                # Nothing succeeded. Exiting 0 here would tell a calling script
                # that the run was fine when in fact every alpha failed.
                log.error(
                    "No alpha completed: %s",
                    ", ".join(f"{r.alpha_id}={r.status}" for r in results),
                )
                exit_code = EXIT_RUNTIME_FAILURE

        except KeyboardInterrupt:
            log.warning(
                "Interrupted by user. Progress is saved in %s; re-run with "
                "--resume-only to finish the simulations already submitted.",
                config.storage.db_path,
            )
            exit_code = EXIT_RUNTIME_FAILURE
        except WorldQuantError as exc:
            log.error("Run failed: %s", exc)
            exit_code = EXIT_RUNTIME_FAILURE

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
