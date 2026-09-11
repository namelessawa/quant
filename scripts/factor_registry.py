#!/usr/bin/env python3
"""Factor Registry / Alpha Memory System — command line interface.

All commands operate on the independent ``data/factor_registry.db`` and never
touch ``worldquant.db`` except ``migrate`` (which only *reads* historical
results from it).

Commands
--------
stats                 Print total counts and the 10-state distribution.
migrate               One-way, idempotent seed from data/worldquant.db.
import-submitted      Pull GET /users/self/alphas and upsert SUBMITTED rows.
similar               Find research-set factors most similar to an expression.
families              Per-factor-family trial / pass / submission table.
report                Write a Markdown memory report (reports/ by default).

Examples
--------
::

    python scripts/factor_registry.py stats
    python scripts/factor_registry.py migrate
    python scripts/factor_registry.py import-submitted
    python scripts/factor_registry.py similar --expression "rank(ts_delta(close,21))"
    python scripts/factor_registry.py families
    python scripts/factor_registry.py report
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from worldquant import (  # noqa: E402
    WorldQuantClient,
    get_logger,
    import_submitted_factors,
    load_config,
    migrate_existing_results,
    require_credentials,
    setup_logging,
)
from worldquant.config import ConfigError  # noqa: E402
from worldquant.registry import (  # noqa: E402
    FactorStatus,
    build_generation_context,
    get_cluster_representatives,
)
from worldquant.registry.adapter import open_registry  # noqa: E402

EXIT_OK = 0
EXIT_USAGE_ERROR = 2


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factor_registry.py",
        description="Factor Registry / Alpha Memory System management CLI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", "-c", help="path to a YAML/JSON config file")
    parser.add_argument("--credentials", help="path to a JSON credentials file")
    parser.add_argument("--db", help="override the registry SQLite path")
    parser.add_argument("--log-level", default="INFO", help="DEBUG / INFO / WARNING")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="print registry totals and state distribution")
    sub.add_parser("migrate", help="idempotently seed from data/worldquant.db")

    submitted = sub.add_parser("import-submitted", help="import the account's live alphas")
    submitted.add_argument("--max-pages", type=int, default=None,
                           help="cap the number of /users/self/alphas pages")

    similar = sub.add_parser("similar", help="find factors similar to an expression")
    similar.add_argument("--expression", "-e", required=True)
    similar.add_argument("--top", "-k", type=int, default=10)
    similar.add_argument("--setting", action="append", default=[], metavar="KEY=VALUE",
                         help="override one setting (repeatable), e.g. --setting delay=0")
    similar.add_argument("--all-statuses", action="store_true",
                         help="search every status, not just the research set")

    families = sub.add_parser("families", help="factor-family trial/pass table")
    families.add_argument("--min-trials", type=int, default=1)

    sub.add_parser("context", help="print the compact generation-context memory block")

    report = sub.add_parser("report", help="write a Markdown registry report")
    report.add_argument("--output", "-o", help="output path (default reports/...)")

    return parser


def parse_setting_overrides(pairs: list[str]) -> dict[str, Any]:
    """Coerce ``KEY=VALUE`` pairs into typed settings (mirrors search_alpha)."""
    overrides: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--setting expects KEY=VALUE, got {pair!r}")
        key, _, raw = pair.partition("=")
        value = raw.strip()
        if value.lower() in {"true", "false"}:
            overrides[key.strip()] = value.lower() == "true"
            continue
        try:
            overrides[key.strip()] = int(value)
            continue
        except ValueError:
            pass
        try:
            overrides[key.strip()] = float(value)
            continue
        except ValueError:
            pass
        overrides[key.strip()] = value
    return overrides


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_stats(registry, args, log) -> int:
    stats = registry.stats()
    print(f"Factor registry: {registry.db_path}")
    print(f"Total factors      : {stats['total']}")
    print(f"  simulated (all)  : {stats['simulated']}")
    print(f"  passed           : {stats['passed']}")
    print(f"  submitted        : {stats['submitted']}")
    print(f"  metric rejected  : {stats['metric_rejected']}")
    print(f"  corr rejected    : {stats['correlation_rejected']}")
    print(f"  duplicates       : {stats['duplicates_blocked']}")
    print(f"  sim failed       : {stats['simulation_failed']}")
    print(f"Features indexed   : {stats['features_indexed']}")
    print("Full state distribution:")
    for status, count in sorted(stats["status_counts"].items()):
        print(f"  {status:<18} {count}")
    return EXIT_OK


def cmd_migrate(registry, config, args, log) -> int:
    summary = migrate_existing_results(config.storage.db_path, registry, log=log)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_import_submitted(registry, config, args, log) -> int:
    credentials = require_credentials(config)
    with WorldQuantClient(
        credentials,
        base_url=config.base_url,
        retry=config.retry,
        min_request_interval=config.runner.min_request_interval,
    ) as client:
        summary = import_submitted_factors(
            client, registry, max_pages=args.max_pages, log=log
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_similar(registry, config, args, log) -> int:
    settings = dict(config.settings)
    settings.update(parse_setting_overrides(args.setting))
    rows = registry.find_similar(
        args.expression,
        settings,
        top_k=args.top,
        statuses=None if args.all_statuses else FactorStatus.RESEARCH_SET,
    )
    exact = registry.find_exact(args.expression, settings)
    if exact:
        print(f"EXACT MATCH: factor_id={exact['id']} status={exact['status']} "
              f"brain_alpha_id={exact.get('brain_alpha_id')}")
    else:
        print("No exact expression+settings match.")
    print(f"Top {len(rows)} similar factors:")
    for item in rows:
        print(
            f"  id={item.factor_id:<5} sim={item.similarity:5.2f} "
            f"{item.status:<16} fam={item.factor_family or '?':<16} "
            f"{item.expression[:70]}"
        )
    return EXIT_OK


def cmd_families(registry, args, log) -> int:
    rows = [r for r in registry.get_family_stats() if int(r["trials"]) >= args.min_trials]
    header = f"{'family':<22} {'trials':>6} {'passed':>6} {'submitted':>9} {'corr_rej':>8} {'avg_sharpe':>10} {'best_fit':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        avg = f"{row['avg_sharpe']:.2f}" if row["avg_sharpe"] is not None else "n/a"
        best = f"{row['best_fitness']:.2f}" if row["best_fitness"] is not None else "n/a"
        print(
            f"{(row['family'] or '?'):<22} {row['trials']:>6} {row['passed']:>6} "
            f"{row['submitted']:>9} {row['corr_rejected']:>8} {avg:>10} {best:>8}"
        )
    return EXIT_OK


def cmd_context(registry, args, log) -> int:
    context = build_generation_context(registry)
    print(json.dumps(context, indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #
def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _structure_usage(registry, *, limit: int = 10) -> list[tuple[str, int]]:
    rows = registry._query(
        """
        SELECT f.field_template AS structure, COUNT(*) AS n
        FROM factors f
        WHERE f.status != ?
        GROUP BY f.field_template
        ORDER BY n DESC
        LIMIT ?
        """,
        (FactorStatus.DUPLICATE, limit),
    )
    return [(row["structure"] or "?", int(row["n"])) for row in rows]


def _field_usage(registry, *, limit: int = 15) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    rows = registry._query(
        "SELECT fields_json FROM factor_features WHERE fields_json IS NOT NULL"
    )
    for row in rows:
        try:
            counter.update(json.loads(row["fields_json"]))
        except (json.JSONDecodeError, TypeError):
            continue
    return counter.most_common(limit)


def _recent(registry, status: str, *, limit: int = 10) -> list[dict[str, Any]]:
    return registry.get_factors_by_status(status, limit=limit)


def build_markdown_report(registry) -> str:
    stats = registry.stats()
    lines: list[str] = []
    lines.append("# Factor Registry / Alpha Memory Report")
    lines.append("")
    lines.append("## 1. Total")
    lines.append("")
    lines.append(f"- Total factors tracked: **{stats['total']}**")
    lines.append(f"- Simulated (any terminal research state): **{stats['simulated']}**")
    lines.append(f"- Passed gate: **{stats['passed']}**")
    lines.append(f"- Submitted: **{stats['submitted']}**")
    lines.append(f"- Feature rows indexed: {stats['features_indexed']}")
    lines.append("")

    lines.append("## 2. Status Distribution")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("| --- | ---: |")
    for status, count in sorted(stats["status_counts"].items()):
        lines.append(f"| {status} | {count} |")
    lines.append("")

    lines.append("## 3. Submitted Alphas")
    lines.append("")
    submitted = _recent(registry, FactorStatus.SUBMITTED, limit=50)
    if not submitted:
        lines.append("_No submitted factors recorded. Run `import-submitted`.")
    else:
        lines.append(f"{len(submitted)} most recent SUBMITTED factor(s):")
        lines.append("")
        lines.append("| ID | Brain alpha | Expression |")
        lines.append("| ---: | --- | --- |")
        for row in submitted:
            expr = (row.get("expression") or "").replace("|", "\\|")[:90]
            lines.append(f"| {row['id']} | {row.get('brain_alpha_id') or 'n/a'} | `{expr}` |")
    lines.append("")

    family_rows = registry.get_family_stats()
    lines.append("## 4. Factor Families — Most and Least Tried")
    lines.append("")
    if not family_rows:
        lines.append("_No family data yet. Run `migrate` or a gated search._")
    else:
        lines.append("| Family | Trials | Passed | Submitted | Avg sharpe | Best fitness |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        ordered = sorted(family_rows, key=lambda r: -int(r["trials"]))
        for row in ordered[:10]:
            lines.append(
                f"| {row['family'] or '?'} | {row['trials']} | {row['passed']} | "
                f"{row['submitted']} | {_fmt(row['avg_sharpe'])} | "
                f"{_fmt(row['best_fitness'])} |"
            )
        lines.append("")
        lines.append("Underexplored (trials <= 5):")
        for row in registry.get_underexplored_families(max_trials=5)[:10]:
            lines.append(f"- {row['family'] or '?'} ({row['trials']} trial(s))")
    lines.append("")

    lines.append("## 5. Most Common Structures (field templates)")
    lines.append("")
    for structure, count in _structure_usage(registry):
        lines.append(f"- `{structure}` — {count}")
    lines.append("")

    lines.append("## 6. Most Used Data Fields")
    lines.append("")
    for field_name, count in _field_usage(registry):
        lines.append(f"- `{field_name}` — {count}")
    lines.append("")

    lines.append("## 7. Correlation Clusters and Representatives")
    lines.append("")
    threshold = registry.config.clustering.corr_threshold
    clusters = get_cluster_representatives(registry, threshold=threshold)
    multi = [c for c in clusters if c["size"] > 1]
    lines.append(f"Threshold {threshold:.2f}; {len(clusters)} cluster(s), "
                 f"{len(multi)} with more than one member.")
    lines.append("")
    if multi:
        lines.append("| Representative | Size | Submitted | Status | Sharpe | Fitness | Expression |")
        lines.append("| ---: | ---: | ---: | --- | ---: | ---: | --- |")
        for cluster in multi[:20]:
            expr = (cluster.get("expression") or "").replace("|", "\\|")[:70]
            lines.append(
                f"| {cluster['representative_id']} | {cluster['size']} | "
                f"{cluster['submitted_count']} | {cluster['status']} | "
                f"{_fmt(cluster['sharpe'])} | {_fmt(cluster['fitness'])} | `{expr}` |"
            )
        lines.append("")

    lines.append("## 8. Saturated and Successful Combinations")
    lines.append("")
    combo_rows = registry.get_combination_stats()
    if not combo_rows:
        lines.append("_No binary family combinations recorded yet._")
    else:
        saturated = registry.get_saturated_combinations()
        successful = [r for r in combo_rows if int(r["submission_count"]) > 0]
        lines.append("Saturated (>=10 trials, <=10% submission rate):")
        if saturated:
            for row in saturated:
                lines.append(
                    f"- `{row['combination_key']}` — {row['trial_count']} trials, "
                    f"{row['submission_count']} submitted, "
                    f"avg sharpe {_fmt(row['avg_sharpe'])}"
                )
        else:
            lines.append("- _none_")
        lines.append("")
        lines.append("Successful (at least one submission):")
        if successful:
            for row in successful:
                lines.append(
                    f"- `{row['combination_key']}` — {row['trial_count']} trials, "
                    f"{row['submission_count']} submitted, "
                    f"best avg sharpe {_fmt(row['avg_sharpe'])}"
                )
        else:
            lines.append("- _none_")
    lines.append("")

    lines.append("## 9. Recent Rejections")
    lines.append("")
    lines.append("### Duplicate / low-novelty blocks")
    dup_rows = _recent(registry, FactorStatus.DUPLICATE, limit=10)
    if dup_rows:
        for row in dup_rows:
            lines.append(
                f"- id={row['id']} `{(row.get('expression') or '')[:80]}`"
                + (f" — {row.get('rejection_reason')}" if row.get("rejection_reason") else "")
            )
    else:
        lines.append("- _none_")
    lines.append("")
    lines.append("### Correlation rejections")
    corr_rows = _recent(registry, FactorStatus.CORR_REJECTED, limit=10)
    if corr_rows:
        for row in corr_rows:
            lines.append(
                f"- id={row['id']} `{(row.get('expression') or '')[:80]}`"
                + (f" — {row.get('rejection_reason')}" if row.get("rejection_reason") else "")
            )
    else:
        lines.append("- _none_")
    lines.append("")
    return "\n".join(lines)


def cmd_report(registry, config, args, log) -> int:
    output = Path(args.output) if args.output else (
        PROJECT_ROOT / "reports" / "factor_registry_report.md"
    )
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_markdown_report(registry), encoding="utf-8")
    print(f"report written to {output}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(None, args.log_level)
    log = get_logger("registry.cli")

    try:
        config_path = args.config
        if config_path is None and (PROJECT_ROOT / "config.yaml").exists():
            config_path = PROJECT_ROOT / "config.yaml"
        config = load_config(config_path, credentials_path=args.credentials)
        if args.db:
            config = replace(
                config,
                registry=replace(config.registry, enabled=True, db_path=Path(args.db)),
            )
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return EXIT_USAGE_ERROR

    if not config.registry.enabled:
        log.error(
            "factor registry is disabled in config (factor_registry.enabled). "
            "Enable it or pass --db PATH."
        )
        return EXIT_USAGE_ERROR

    registry = open_registry(config, log=log)
    if registry is None:
        log.error("could not open factor registry")
        return EXIT_USAGE_ERROR

    try:
        with registry:
            if args.command == "stats":
                return cmd_stats(registry, args, log)
            if args.command == "migrate":
                return cmd_migrate(registry, config, args, log)
            if args.command == "import-submitted":
                return cmd_import_submitted(registry, config, args, log)
            if args.command == "similar":
                return cmd_similar(registry, config, args, log)
            if args.command == "families":
                return cmd_families(registry, args, log)
            if args.command == "context":
                return cmd_context(registry, args, log)
            if args.command == "report":
                return cmd_report(registry, config, args, log)
            log.error("unknown command: %s", args.command)
            return EXIT_USAGE_ERROR
    except (ConfigError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
