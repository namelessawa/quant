#!/usr/bin/env python3
"""Mark alphas as permanently excluded from the usable-alpha pool.

Usage:
    # Mark the 7 analyst-family alphas that failed SELF_CORRELATION in the
    # one-by-one screen on 2026-09-08.
    python scripts/mark_excluded.py --reason SELF_CORRELATION_BLOCKED \
        --alpha-id npKzLemd --alpha-id YP5Wj6Rl --alpha-id Vk6YvwW8 \
        --alpha-id kqVL1PLd --alpha-id 58QqvJkz --alpha-id Vk6Y8xXM \
        --alpha-id O0r5pPgq

    # Clear the tag again (re-enable an alpha):
    python scripts/mark_excluded.py --reason SELF_CORRELATION_BLOCKED \
        --alpha-id XXX --clear

The tag is stored in `results.excluded_reason` and surfaces in the
experiment ledger as the `excluded_reason` column. `rank_results` and the
search's `--include-existing` pool skip any alpha carrying a non-empty tag.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from worldquant import load_config, setup_logging, get_logger
from worldquant.storage import ResultStore

#: The seven GOOD+ analyst-family alphas that 7/8 PASS the submission checks
#: but fail only SELF_CORRELATION, after the 2026-09-08 one-by-one screen
#: using GET /alphas/{id}/check. Their SELF_CORRELATION values (0.7206 to
#: 1.0000) are all over the 0.7 limit, and re-polling on the same day showed
#: the verdict is stable — the analyst family duplicates 2rOn70lb too closely
#: to ever submit alongside it.
SELF_CORRELATION_BLOCKED_DEFAULTS: tuple[str, ...] = (
    "npKzLemd",
    "YP5Wj6Rl",
    "Vk6YvwW8",
    "kqVL1PLd",
    "58QqvJkz",
    "Vk6Y8xXM",
    "O0r5pPgq",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reason",
        default="SELF_CORRELATION_BLOCKED",
        help="Exclusion tag to attach (default: %(default)s)",
    )
    parser.add_argument(
        "--alpha-id",
        action="append",
        dest="alpha_ids",
        help="Remote alpha id to mark. May be passed multiple times. "
        "Pass --use-defaults to mark the 2026-09-08 screened analyst family.",
    )
    parser.add_argument(
        "--use-defaults",
        action="store_true",
        help=f"Mark the {len(SELF_CORRELATION_BLOCKED_DEFAULTS)} known "
        "SELF_CORRELATION_BLOCKED alphas from the 2026-09-08 screen.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove the exclusion tag instead of setting it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(None)
    setup_logging(config.storage.log_file, "INFO")
    log = get_logger("mark_excluded")

    if args.use_defaults:
        ids = list(SELF_CORRELATION_BLOCKED_DEFAULTS)
    else:
        ids = list(args.alpha_ids or [])
    if not ids:
        log.error(
            "No alpha ids given. Pass --alpha-id ID repeatedly, or --use-defaults."
        )
        return 2

    verb = "Clearing" if args.clear else "Setting"
    log.info(
        "%s excluded_reason=%s on %d alpha(s): %s",
        verb, args.reason, len(ids), ", ".join(ids),
    )

    with ResultStore(config.storage.db_path) as store:
        for alpha_id in ids:
            ok = store.mark_excluded(alpha_id, args.reason, clear=args.clear)
            label = "cleared" if args.clear else "marked"
            if ok:
                log.info("  %s %s", alpha_id, label)
            else:
                log.warning("  %s not found in completed results", alpha_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
