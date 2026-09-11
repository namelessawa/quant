#!/usr/bin/env python3
"""Re-run BRAIN's submission checks for alphas that are already in the database.

``search_alpha.py`` checks every alpha as it simulates it. This script covers the
two cases that cannot:

- rows simulated **before** the check gate existed, which carry no verdict at all
  and are therefore never reported by ``--include-existing``;
- ``SELF_CORRELATION``, which is **not stable** — it measures an alpha against
  everything else in the account, so one that passed at 0.6996 can fail later
  just because more alphas were added.

Nothing is simulated and no quota is spent: ``GET /alphas/{id}/check`` is a read.

Examples
--------
::

    # every completed alpha that has no recorded verdict
    python scripts/recheck_submissions.py

    # only the GOOD-or-better ones
    python scripts/recheck_submissions.py --min-grade GOOD

    # refresh everything, including rows that already passed
    python scripts/recheck_submissions.py --all

    # one specific alpha
    python scripts/recheck_submissions.py --alpha-id 2rOn70lb
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from worldquant import (  # noqa: E402
    AlphaGrade,
    ResultStore,
    WorldQuantClient,
    WorldQuantError,
    get_logger,
    load_config,
    require_credentials,
    setup_logging,
)
from worldquant.api import SUBMISSION_CHECKS, SimulationStatus, passed_check_count  # noqa: E402
from worldquant.config import ConfigError  # noqa: E402

EXIT_OK = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_USAGE_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-run GET /alphas/{id}/check for stored alphas and record the verdict.",
    )
    parser.add_argument(
        "--min-grade",
        default=None,
        choices=[grade for grade in AlphaGrade.OBSERVED_ORDER],
        help="only re-check alphas graded this or better",
    )
    parser.add_argument(
        "--alpha-id",
        action="append",
        default=None,
        help="re-check one specific remote alpha id; repeatable",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="re-check every stored alpha, including ones that already have a verdict",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--credentials", default=None, help="path to credentials.json")
    parser.add_argument("--db", default=None, help="override the SQLite path")
    parser.add_argument("--log-file", default=None, help="override the log file path")
    parser.add_argument("--log-level", default=None)
    parser.add_argument(
        "--min-request-interval",
        type=float,
        default=None,
        help="seconds between requests; raise it if BRAIN answers 429",
    )
    return parser


def grade_at_least(grade: str | None, floor: str | None) -> bool:
    """True when ``grade`` reaches ``floor``. No floor means everything qualifies."""
    if floor is None:
        return True
    if not grade:
        return False
    return AlphaGrade.rank(grade) >= AlphaGrade.rank(floor)


def select_targets(results, *, min_grade: str | None, alpha_ids: list[str] | None,
                   include_verified: bool) -> list:
    """Pick which stored results to re-check, one row per alpha.

    An explicitly named ``--alpha-id`` bypasses both filters: naming an alpha is
    a request to check that alpha, whatever its grade and whether or not it was
    checked before. The filters only apply when selecting by criteria.
    """
    wanted = set(alpha_ids) if alpha_ids else None
    chosen: list = []
    seen: set[str] = set()

    for result in results:
        alpha_id = result.remote_alpha_id
        if not alpha_id or alpha_id in seen:
            continue

        if wanted is not None:
            if alpha_id not in wanted:
                continue
        else:
            if not grade_at_least(result.grade, min_grade):
                continue
            # An existing verdict is only refreshed on request: re-checking all
            # 100+ rows when one was asked for wastes requests against an
            # aggressive rate limiter.
            if result.submission_checks and not include_verified:
                continue

        seen.add(alpha_id)
        chosen.append(result)

    if wanted is not None:
        missing = sorted(wanted - seen)
        if missing:
            raise ConfigError(
                "no completed result stored for alpha id(s): " + ", ".join(missing)
            )
    return chosen


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provisional = setup_logging(None, "INFO")

    overrides: dict[str, object] = {}
    if args.min_request_interval is not None:
        overrides["min_request_interval"] = args.min_request_interval

    try:
        config = load_config(args.config, overrides=overrides, credentials_path=args.credentials)
        storage_overrides = {
            key: Path(value)
            for key, value in (("db_path", args.db), ("log_file", args.log_file))
            if value
        }
        if storage_overrides:
            config = replace(config, storage=replace(config.storage, **storage_overrides))
        setup_logging(config.storage.log_file, args.log_level or config.storage.log_level)
        credentials = require_credentials(config)
    except ConfigError as exc:
        provisional.error("Configuration error: %s", exc)
        return EXIT_USAGE_ERROR

    log = get_logger("recheck")

    with ResultStore(config.storage.db_path) as store:
        results = store.all_results(statuses=[SimulationStatus.COMPLETED])
        try:
            targets = select_targets(
                results,
                min_grade=args.min_grade,
                alpha_ids=args.alpha_id,
                include_verified=args.all,
            )
        except ConfigError as exc:
            log.error("%s", exc)
            return EXIT_USAGE_ERROR

        if not targets:
            log.info(
                "Nothing to re-check among %d completed result(s). Use --all to "
                "refresh alphas that already carry a verdict.",
                len(results),
            )
            return EXIT_OK

        log.info(
            "Re-checking %d alpha(s) against GET /alphas/{id}/check (read-only, "
            "no simulation quota spent)",
            len(targets),
        )

        submittable: list[str] = []
        rejected: list[tuple[str, str]] = []
        unresolved: list[str] = []

        with WorldQuantClient(
            credentials,
            base_url=config.base_url,
            retry=config.retry,
            min_request_interval=config.runner.min_request_interval,
        ) as client:
            try:
                client.ensure_authenticated()
            except WorldQuantError as exc:
                log.error("Cannot log in: %s", exc)
                return EXIT_RUNTIME_FAILURE

            for index, result in enumerate(targets, start=1):
                alpha_id = result.remote_alpha_id
                try:
                    checked = client.check_submission(alpha_id)
                except WorldQuantError as exc:
                    log.warning("%s: check failed (%s); leaving the stored row untouched",
                                alpha_id, exc)
                    unresolved.append(alpha_id)
                    continue

                checks = checked.get("checks") or {}
                if not checks:
                    log.warning("%s: endpoint returned no checks; leaving the row untouched",
                                alpha_id)
                    unresolved.append(alpha_id)
                    continue

                store.record_submission_check(alpha_id, checks, checked.get("self_correlation"))
                passed = passed_check_count(checks)
                if passed == len(SUBMISSION_CHECKS):
                    submittable.append(alpha_id)
                    log.info(
                        "[%d/%d] %-10s %-11s %d/%d PASS  self-correlation=%s",
                        index, len(targets), alpha_id, result.grade or "n/a",
                        passed, len(SUBMISSION_CHECKS),
                        f"{checked.get('self_correlation'):.4f}"
                        if checked.get("self_correlation") is not None else "n/a",
                    )
                else:
                    reasons = "; ".join(checked.get("failures") or [])
                    rejected.append((alpha_id, reasons))
                    log.warning(
                        "[%d/%d] %-10s %-11s %d/%d PASS  NOT submittable - %s",
                        index, len(targets), alpha_id, result.grade or "n/a",
                        passed, len(SUBMISSION_CHECKS), reasons,
                    )

    log.info("=" * 64)
    log.info("Re-checked %d alpha(s): %d submittable, %d not, %d unresolved",
             len(targets), len(submittable), len(rejected), len(unresolved))
    for alpha_id, reasons in rejected:
        log.info("  NOT submittable: %s - %s", alpha_id, reasons)
    if submittable or rejected:
        log.info(
            "Verdicts are in the database. Refresh the xlsx ledger with: "
            "python scripts/run_backtest.py --export-only --rebuild-ledger"
        )
    log.info("=" * 64)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
