#!/usr/bin/env python3
"""Generate candidate alpha expressions WITHOUT submitting them.

Generation and submission are separate steps on purpose: produce a candidate
list here, review and prune it, then feed it to ``run_backtest.py --input``.
Nothing in this script contacts WorldQuant BRAIN.

Example
-------
::

    python scripts/generate_alphas.py --output data/candidates.csv \\
        --operators ts_mean ts_std_dev ts_delta \\
        --fields close volume returns \\
        --windows 5 10 20 60
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from worldquant.generator import (  # noqa: E402
    DEFAULT_FIELDS,
    DEFAULT_OPERATORS,
    DEFAULT_TEMPLATE,
    DEFAULT_WINDOWS,
    DEFAULT_WRAPPER,
    CombinatorialGenerator,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_alphas.py",
        description="Write candidate alpha expressions to a file. Does not submit anything.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", "-o", help="output .csv or .txt path (omit with --dry-run)")
    parser.add_argument("--operators", nargs="+", default=list(DEFAULT_OPERATORS))
    parser.add_argument("--fields", nargs="+", default=list(DEFAULT_FIELDS))
    parser.add_argument("--windows", nargs="+", type=int, default=list(DEFAULT_WINDOWS))
    parser.add_argument("--wrapper", default=DEFAULT_WRAPPER, help="outer operator, e.g. rank")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE, help="expression template")
    parser.add_argument(
        "--max-count", type=int, default=None,
        help="cap the number of generated candidates",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the candidates instead of writing a file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.output and not args.dry_run:
        print(
            "error: pass --output PATH to write candidates, or --dry-run to print them",
            file=sys.stderr,
        )
        return 2

    generator = CombinatorialGenerator(
        operators=args.operators,
        fields=args.fields,
        windows=args.windows,
        wrapper=args.wrapper,
        template=args.template,
        max_count=args.max_count,
    )
    expressions = generator.generate()

    if not expressions:
        print("No candidates generated.", file=sys.stderr)
        return 1

    if args.dry_run:
        for expression in expressions:
            print(expression)
        print(f"\n{len(expressions)} candidate(s); nothing written.", file=sys.stderr)
        return 0

    output = Path(args.output)
    generator.write(output)
    print(f"Wrote {len(expressions)} candidate(s) to {output}")
    print("Review the list, then run: python scripts/run_backtest.py --input", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
