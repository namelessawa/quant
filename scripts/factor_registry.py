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
explain               Full fingerprint/L0-L3 verdict for a candidate expression.
why                   Explain why a stored factor was rejected/accepted.
families              Per-factor-family trial / pass / submission table.
report                Write a Markdown memory report (reports/ by default).

Examples
--------
::

    python scripts/factor_registry.py stats
    python scripts/factor_registry.py migrate
    python scripts/factor_registry.py import-submitted
    python scripts/factor_registry.py similar --expression "rank(ts_delta(close,21))"
    python scripts/factor_registry.py explain -e "group_zscore(ts_delta(close,5),subindustry)"
    python scripts/factor_registry.py why --id 123
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
    fetch_field_metadata,
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
    get_clusters,
)
from worldquant.registry.adapter import open_registry  # noqa: E402
from worldquant.registry.refresh import (  # noqa: E402
    backfill_correlations,
    refresh_unknown_correlations,
)

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

    explain = sub.add_parser(
        "explain", help="fingerprints + L0/L1/L2/L3 verdict for a candidate")
    explain.add_argument("--expression", "-e", required=True)
    explain.add_argument("--set-json", default=None,
                         help="settings as a JSON object")
    explain.add_argument("--setting", action="append", default=[], metavar="KEY=VALUE",
                         help="override one setting (repeatable)")
    explain.add_argument("--source", default=None,
                         help="candidate source, e.g. 'ablation'")
    explain.add_argument("--force", action="store_true",
                         help="evaluate as if hard rejects are overridden")
    explain.add_argument("--top", "-k", type=int, default=5,
                         help="nearest factors to show")

    why = sub.add_parser("why", help="explain the fate of a stored factor")
    why.add_argument("--alpha-id", default=None, help="BRAIN remote alpha id")
    why.add_argument("--id", dest="factor_id", type=int, default=None,
                     help="local factor id")

    families = sub.add_parser("families", help="factor-family trial/pass table")
    families.add_argument("--min-trials", type=int, default=1)

    sub.add_parser("context", help="print the compact generation-context memory block")

    report = sub.add_parser("report", help="write a Markdown registry report")
    report.add_argument("--output", "-o", help="output path (default reports/...)")

    refresh = sub.add_parser(
        "refresh-corr",
        help="re-check SIMULATED factors with UNKNOWN corr via GET .../check "
             "(read-only; no simulation, no submission)",
    )
    refresh.add_argument("--limit", type=int, default=50,
                         help="maximum factors to refresh this run")
    refresh.add_argument("--alpha-id", default=None,
                         help="refresh one specific BRAIN alpha id")

    backfill = sub.add_parser(
        "backfill-corr",
        help="pull per-neighbor SELF edges for factors that only have SELF_MAX "
             "(read-only; no simulation)",
    )
    backfill.add_argument("--limit", type=int, default=100,
                          help="maximum factors to backfill this run")

    fetch_fields = sub.add_parser(
        "fetch-fields",
        help="fetch metadata for fields used by stored factors via "
             "GET /data-fields/{id} (read-only; never the full field library)",
    )
    fetch_fields.add_argument("--limit", type=int, default=None,
                              help="cap the number of fields fetched this run")
    fetch_fields.add_argument("--all", action="store_true", dest="refetch_all",
                              help="re-fetch even fields already cached")

    clusters = sub.add_parser(
        "clusters",
        help="print correlation clusters built from real pairwise edges",
    )
    clusters.add_argument("--min-size", type=int, default=1,
                          help="hide clusters smaller than this")
    clusters.add_argument("--threshold", type=float, default=None,
                          help="edge |corr| cutoff (default: config clustering)")

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


def _with_client(config, fn):
    """Run ``fn(client)`` inside an authenticated BRAIN client session."""
    credentials = require_credentials(config)
    with WorldQuantClient(
        credentials,
        base_url=config.base_url,
        retry=config.retry,
        min_request_interval=config.runner.min_request_interval,
    ) as client:
        client.ensure_authenticated()
        return fn(client)


def cmd_refresh_corr(registry, config, args, log) -> int:
    summary = _with_client(
        config,
        lambda client: refresh_unknown_correlations(
            client, registry, limit=args.limit, alpha_id=args.alpha_id, log=log
        ),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_backfill_corr(registry, config, args, log) -> int:
    summary = _with_client(
        config,
        lambda client: backfill_correlations(
            client, registry, limit=args.limit, log=log
        ),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_fetch_fields(registry, config, args, log) -> int:
    summary = _with_client(
        config,
        lambda client: fetch_field_metadata(
            client, registry,
            only_missing=not args.refetch_all,
            limit=args.limit, log=log,
        ),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_clusters(registry, config, args, log) -> int:
    clusters = get_clusters(
        registry, threshold=args.threshold, min_size=args.min_size
    )
    if not clusters:
        print("No correlation clusters (no pairwise edges above the cutoff).")
        return EXIT_OK
    header = (
        f"{'cluster':>7} {'size':>4} {'representative':>14} "
        f"{'submitted':>9} {'avg_corr':>8} {'mean|r|':>8} {'cov':>5} "
        f"{'dens':>5} {'max_corr':>8} {'saturated':>9}  theme"
    )
    print(header)
    print("-" * len(header))
    for cluster in clusters:
        avg = f"{cluster.avg_corr:.3f}" if cluster.avg_corr is not None else "n/a"
        mean = (
            f"{cluster.mean_abs_corr:.3f}"
            if cluster.mean_abs_corr is not None else "n/a"
        )
        cov = (
            f"{cluster.known_pair_coverage:.2f}"
            if cluster.known_pair_coverage is not None else "n/a"
        )
        dens = (
            f"{cluster.high_corr_density:.2f}"
            if cluster.high_corr_density is not None else "n/a"
        )
        mx = f"{cluster.max_corr:.3f}" if cluster.max_corr is not None else "n/a"
        print(
            f"{cluster.cluster_id:>7} {cluster.size:>4} "
            f"{cluster.representative_id:>14} {cluster.submitted_count:>9} "
            f"{avg:>8} {mean:>8} {cov:>5} {dens:>5} {mx:>8} "
            f"{str(cluster.saturated):>9}  {cluster.theme or ''}"
        )
    print(
        f"\n{len(clusters)} cluster(s); edges are pairwise SELF rows only "
        "(SELF_MAX aggregates never enter the graph). avg_corr averages graph "
        "edges (>= cutoff) for compatibility; mean|r| averages ALL observed "
        "member pairs, cov=known pairs/all pairs, dens=high-corr pairs/all pairs."
    )
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


def cmd_explain(registry, config, args, log) -> int:
    settings = dict(config.settings)
    if args.set_json:
        try:
            payload = json.loads(args.set_json)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"--set-json is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError("--set-json must be a JSON object")
        settings.update(payload)
    settings.update(parse_setting_overrides(args.setting))

    fp = registry.fingerprints(args.expression, settings)
    features = fp["features"]
    verdict = registry.evaluate_candidate(
        args.expression,
        settings,
        source=args.source,
        force=args.force,
    )
    neighbors = registry.find_similar(
        args.expression, settings,
        top_k=args.top, statuses=FactorStatus.RESEARCH_SET,
    )

    recommendation = {
        "REJECT_EXACT_DUPLICATE": "Already run with identical settings — do NOT re-simulate.",
        "SKIP_SIGNAL_DUPLICATE": (
            "Same signal already swept on parameters — change data/legs/family, "
            "not truncation/decay/window."
        ),
        "SKIP_LOW_NOVELTY": "Structure is saturated and novelty is low — pick a new template/dataset.",
        "SIMULATE": "No blocking duplicate; eligible for simulation.",
    }.get(verdict["action"], verdict["action"])

    payload = {
        "expression_input": args.expression,
        "canonical_expression": fp["canonical"],
        "scope_settings": fp["scope_settings"],
        "fingerprints": verdict["hashes"],
        "templates": {
            "field_template": fp["field_template"],
            "family_template": fp["family_template"],
            "structure_hash": fp["structure_hash"],
            "structure": features.structure,
        },
        "features": {
            "fields": features.fields,
            "field_families": fp["field_families"],
            "factor_family": fp["factor_family"],
            "operators": features.operators,
            "operator_multiset": features.operator_multiset,
            "operator_paths": features.operator_paths,
            "windows": features.windows,
            "tree_depth": features.tree_depth,
            "root_operator": features.root_operator,
            "subtrees": features.subtrees,
        },
        "combinations": [
            c.to_dict() if hasattr(c, "to_dict") else dict(c)
            for c in fp["combinations"]
        ],
        "gate": verdict,
        "nearest_factors": [
            {
                "factor_id": item.factor_id,
                "similarity": item.similarity,
                "status": item.status,
                "family": item.factor_family,
                "expression": item.expression,
            }
            for item in neighbors
        ],
        "recommendation": recommendation,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


def cmd_why(registry, config, args, log) -> int:
    if not args.factor_id and not args.alpha_id:
        raise ConfigError("why requires --id or --alpha-id")
    if args.factor_id:
        factor = registry.get_factor(int(args.factor_id))
    else:
        rows = registry._query(
            "SELECT * FROM factors WHERE brain_alpha_id = ?",
            (str(args.alpha_id),),
        )
        factor = dict(rows[0]) if rows else None
    if factor is None:
        print("factor not found", file=sys.stderr)
        return EXIT_USAGE_ERROR

    factor_id = int(factor["id"])
    metrics = registry.get_metrics(factor_id)
    corr_rows = registry.get_nearest_correlated_factors(factor_id, limit=10)
    settings = json.loads(factor.get("settings_json") or "{}")
    context = registry.evaluate_candidate(factor["expression"], settings)

    reason_buckets = {
        "SIMULATION_FAILED": "The simulation itself errored/timed out (see rejection_reason).",
        "METRIC_REJECTED": f"Metric gate failed: {factor.get('failure_category') or 'see checks/reasons'}.",
        "CORR_REJECTED": (
            f"Self-correlation gate ({factor.get('corr_status')}/"
            f"{factor.get('corr_band')}); strong but redundant — change data/legs."
            if factor.get("high_quality_redundant")
            else f"Self-correlation gate rejected it ({factor.get('corr_band')})."
        ),
        "DUPLICATE": "Duplicate/saturated-variant block at the pre-simulation gate.",
        "SIMULATED": "Metrics pass; correlation evidence pending/unknown — not yet submittable.",
        "PASSED": "Passed all gates.",
        "SUBMITTED": "Submitted to BRAIN.",
    }

    payload = {
        "factor_id": factor_id,
        "status": factor["status"],
        "expression": factor["expression"],
        "canonical_expression": factor["canonical_expression"],
        "brain_alpha_id": factor.get("brain_alpha_id"),
        "settings": settings,
        "failure_category": factor.get("failure_category"),
        "rejection_reason": factor.get("rejection_reason"),
        "corr_state": {
            "status": factor.get("corr_status"),
            "band": factor.get("corr_band"),
            "margin_to_limit": factor.get("corr_margin"),
            "nearest_cluster_id": factor.get("nearest_cluster_id"),
            "high_quality_redundant": bool(factor.get("high_quality_redundant")),
        },
        "metrics": metrics,
        "nearest_correlations": corr_rows[:5],
        "memory_context_if_repeated_today": {
            "action": context["action"],
            "same_signal": context["same_signal"],
            "signal_trials": context["signal_trials"],
            "previous_experiments": context["previous_experiments"],
            "novelty": context["novelty"],
            "nearest_cluster": context["nearest_cluster"],
            "subtree_neighbor_count": context["subtree_neighbor_count"],
        },
        "explanation": reason_buckets.get(factor["status"], factor["status"]),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
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


def _correlation_evidence(registry) -> dict:
    """Summarize SELF-correlation evidence availability for the report."""
    brain_linked = int(registry._query(
        "SELECT COUNT(*) AS n FROM factors WHERE brain_alpha_id IS NOT NULL"
    )[0]["n"])
    no_brain = int(registry._query(
        "SELECT COUNT(*) AS n FROM factors WHERE brain_alpha_id IS NULL"
    )[0]["n"])
    self_edges = int(registry._query(
        "SELECT COUNT(*) AS n FROM factor_correlations WHERE correlation_type='SELF'"
    )[0]["n"])
    self_edge_factors = int(registry._query(
        "SELECT COUNT(DISTINCT factor_id) AS n FROM factor_correlations "
        "WHERE correlation_type='SELF'"
    )[0]["n"])

    note_rows = registry._query(
        """
        SELECT CASE
                   WHEN corr_evidence_note LIKE 'GATED:%' THEN 'GATED'
                   ELSE COALESCE(corr_evidence_note, 'LEGACY_FINAL')
               END AS state,
               COUNT(*) AS n
          FROM factors
         WHERE corr_evidence_final_at IS NOT NULL
         GROUP BY state ORDER BY n DESC
        """
    )
    state_counts: dict[str, int] = {
        str(row["state"]): int(row["n"]) for row in note_rows
    }
    pending = int(registry._query(
        """
        SELECT COUNT(*) AS n FROM factors f
         WHERE f.brain_alpha_id IS NOT NULL
           AND f.corr_evidence_final_at IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM factor_correlations c
                WHERE c.factor_id = f.id AND c.correlation_type = 'SELF')
        """
    )[0]["n"])

    ordered_states = [
        "SELF_NEIGHBORS", "SELF_PASS", "SELF_FAIL", "SELF_ERROR",
        "GATED", "ALREADY_SUBMITTED", "LEGACY_FINAL",
    ]
    states = [(s, state_counts.pop(s)) for s in ordered_states
              if state_counts.get(s)]
    states.extend((s, n) for s, n in sorted(state_counts.items()))
    states.append(("PENDING", pending))
    states.append(("NO_BRAIN_ID", no_brain))

    gated = registry._query(
        """
        SELECT substr(corr_evidence_note, 7) AS gates, COUNT(*) AS n
          FROM factors
         WHERE corr_evidence_note LIKE 'GATED:%'
         GROUP BY gates ORDER BY n DESC LIMIT 8
        """
    )
    return {
        "brain_linked": brain_linked,
        "self_edges": self_edges,
        "self_edge_factors": self_edge_factors,
        "states": states,
        "gated_detail": [(str(r["gates"]), int(r["n"])) for r in gated],
    }


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

    lines.append("### Correlation evidence coverage")
    lines.append("")
    cov = _correlation_evidence(registry)
    lines.append(
        f"Brain-linked factors: {cov['brain_linked']}; pairwise SELF edges: "
        f"{cov['self_edges']} across {cov['self_edge_factors']} factor(s)."
    )
    lines.append("")
    lines.append("| Evidence state | Count | Meaning |")
    lines.append("| --- | ---: | --- |")
    meaning = {
        "SELF_NEIGHBORS": "concrete neighbour recordset (real graph edges)",
        "SELF_PASS": "SELF_CORRELATION resolved PASS, zero neighbours",
        "SELF_FAIL": "SELF_CORRELATION resolved FAIL",
        "SELF_ERROR": "platform returned an error verdict",
        "GATED": "fails another submission gate; platform never schedules corr",
        "ALREADY_SUBMITTED": "duplicate alpha; /check exposes no corr",
        "LEGACY_FINAL": "finalized before note classification",
        "PENDING": "corr genuinely still computing / awaiting re-poll",
        "NO_BRAIN_ID": "local factor without a brain alpha id",
    }
    for state, count in cov["states"]:
        lines.append(f"| {state} | {count} | {meaning.get(state, '')} |")
    lines.append("")
    gated_detail = cov["gated_detail"]
    if gated_detail:
        detail = ", ".join(f"{gate}×{n}" for gate, n in gated_detail)
        lines.append(f"Gating gates observed: {detail}")
        lines.append("")
    lines.append(
        "_GATED / ALREADY_SUBMITTED factors keep corr verdict UNKNOWN: the "
        "evidence is not available on the platform, not a PASS/FAIL._"
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

    ctx = build_generation_context(registry)

    lines.append("## 10. Research Coverage")
    lines.append("")
    lines.append("### Underexplored datasets (trials <= 3)")
    if ctx["underexplored_datasets"]:
        for row in ctx["underexplored_datasets"]:
            lines.append(f"- `{row['dataset']}` ({row['trials']} trial(s))")
    else:
        lines.append("- _none — no catalog loaded, or every catalog dataset already has >3 trials_")
    lines.append("")
    lines.append("### Underexplored operator structures (family templates <= 5)")
    if ctx["underexplored_operator_structures"]:
        for row in ctx["underexplored_operator_structures"][:15]:
            lines.append(f"- `{row['family_template']}` ({row['trials']} trial(s))")
    else:
        lines.append("- _none_")
    lines.append("")
    saturated_clusters = ctx["saturated_clusters"]
    cluster_cfg = registry.config.clustering
    lines.append(
        f"### Saturated clusters: {len(saturated_clusters)} "
        f"(min size {cluster_cfg.saturated_min_size}, coverage >= "
        f"{cluster_cfg.saturated_min_coverage:.2f}, mean |corr| >= "
        f"{cluster_cfg.saturated_mean_abs_corr:.2f} OR high-corr density >= "
        f"{cluster_cfg.saturated_high_corr_density:.2f})"
    )
    if saturated_clusters:
        lines.append("")
        lines.append(
            "| Rep | Size | Avg corr (edges) | Mean abs corr | Coverage | "
            "High density | Max corr | Best sharpe | Theme | Families |"
        )
        lines.append(
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"
        )
        for cluster in saturated_clusters[:15]:
            families = ", ".join(
                list((cluster.get("family_distribution") or {}).keys())[:3]
            )
            lines.append(
                f"| {cluster['representative_id']} | {cluster['size']} | "
                f"{_fmt(cluster.get('avg_corr'), 3)} | "
                f"{_fmt(cluster.get('mean_abs_corr'), 3)} | "
                f"{_fmt(cluster.get('known_pair_coverage'), 3)} | "
                f"{_fmt(cluster.get('high_corr_density'), 3)} | "
                f"{_fmt(cluster.get('max_corr'), 3)} | "
                f"{_fmt(cluster.get('best_sharpe'))} | "
                f"{cluster.get('theme') or '?'} | {families} |"
            )
    else:
        lines.append("- _none_")
    lines.append("")

    lines.append("## 11. DO_NOT_REPEAT — Dead Ends")
    lines.append("")
    if ctx["do_not_repeat"]:
        for item in ctx["do_not_repeat"]:
            lines.append(
                f"- **[{item['kind']}]** `{item['pattern']}` — {item['reason']} "
                f"({item['trials']} trial(s), best corr {_fmt(item.get('best_corr'))})"
            )
    else:
        lines.append("- _none recorded_")
    lines.append("")
    lines.append("### Failed research directions (by failure category)")
    if ctx["failed_research_directions"]:
        for row in ctx["failed_research_directions"]:
            families = ", ".join(
                f"{name}×{n}" for name, n in list(row["families"].items())[:3]
            )
            lines.append(
                f"- **{row['label']}** — {row['trials']} failure(s): {families}"
            )
    else:
        lines.append("- _none_")
    lines.append("")

    lines.append("## 12. High-Quality Redundant — Change Data, Not Parameters")
    lines.append("")
    redundant = ctx["high_quality_redundant_examples"]
    if redundant:
        for row in redundant[:10]:
            expr = (row.get("expression") or "").replace("|", "\\|")[:80]
            lines.append(
                f"- id={row['factor_id']} sharpe={_fmt(row.get('sharpe'))} "
                f"fitness={_fmt(row.get('fitness'))} "
                f"margin={_fmt(row.get('corr_margin'), 3)} `{expr}`"
            )
        lines.append("")
        lines.append(
            "_Rule: these signals are real. Keep the idea, switch legs/dataset/"
            "family — do NOT retune window/decay/truncation._"
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
            if args.command == "explain":
                return cmd_explain(registry, config, args, log)
            if args.command == "why":
                return cmd_why(registry, config, args, log)
            if args.command == "families":
                return cmd_families(registry, args, log)
            if args.command == "context":
                return cmd_context(registry, args, log)
            if args.command == "report":
                return cmd_report(registry, config, args, log)
            if args.command == "refresh-corr":
                return cmd_refresh_corr(registry, config, args, log)
            if args.command == "backfill-corr":
                return cmd_backfill_corr(registry, config, args, log)
            if args.command == "fetch-fields":
                return cmd_fetch_fields(registry, config, args, log)
            if args.command == "clusters":
                return cmd_clusters(registry, config, args, log)
            log.error("unknown command: %s", args.command)
            return EXIT_USAGE_ERROR
    except (ConfigError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
