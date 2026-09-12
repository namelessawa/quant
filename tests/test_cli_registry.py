"""run_backtest.py <-> Factor Registry integration (spec section 30).

The real CLI is driven with the fake BRAIN backend from test_cli; these tests
prove that the Registry gate wraps simulation end to end:

* a normal run records the finished alpha in research memory;
* an exact duplicate is gated before BRAIN is contacted (no new simulation);
* ``--force`` re-runs a known experiment deliberately;
* ``--no-registry`` restores the pre-Registry execution path exactly.
"""

from __future__ import annotations

import sqlite3

import pytest

# Reuse the full CLI harness (patched client factory, credentials, tmp paths).
from test_cli import cli  # noqa: F401

EXPR = "rank(ts_delta(close, 5))"
RESEARCHED_STATUSES = {
    "SIMULATED", "PASSED", "SUBMITTED",
    "CORR_REJECTED", "METRIC_REJECTED", "SIMULATION_FAILED",
}


def _enable_registry(cli):
    cfg = cli["tmp_path"] / "registry-on.yaml"
    cfg.write_text(
        "factor_registry:\n"
        "  enabled: true\n"
        "  db_path: factor_registry.db\n",
        encoding="utf-8",
    )
    return cfg


def _write_input(cli, expression=EXPR):
    path = cli["tmp_path"] / "exprs.txt"
    path.write_text(expression + "\n", encoding="utf-8")
    return path


def _registry_rows(cli):
    db = cli["tmp_path"] / "factor_registry.db"
    assert db.exists()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(
            "SELECT id, status, corr_status FROM factors ORDER BY id"
        )]
    finally:
        conn.close()


def _log_text(cli):
    return (cli["tmp_path"] / "cli.log").read_text(encoding="utf-8")


def test_run_records_finished_alpha_into_registry(cli):
    cfg = _enable_registry(cli)
    source = _write_input(cli)

    assert cli["run"]("--config", str(cfg), "--input", str(source)) == 0
    assert cli["backend"].submissions == 1

    rows = _registry_rows(cli)
    assert len(rows) == 1
    assert rows[0]["status"] in RESEARCHED_STATUSES
    assert rows[0]["status"] != "GENERATED"


def test_exact_duplicate_is_gated_before_brain_is_contacted(cli):
    cfg = _enable_registry(cli)
    source = _write_input(cli)

    assert cli["run"]("--config", str(cfg), "--input", str(source)) == 0
    assert cli["backend"].submissions == 1

    # Second identical run: the Registry gate blocks first; no POST /simulations.
    assert cli["run"]("--config", str(cfg), "--input", str(source)) == 0
    assert cli["backend"].submissions == 1
    assert len(_registry_rows(cli)) == 1
    assert "Registry gate skipped 1/1" in _log_text(cli)


def test_force_reruns_known_experiment(cli):
    cfg = _enable_registry(cli)
    source = _write_input(cli)

    assert cli["run"]("--config", str(cfg), "--input", str(source)) == 0
    assert cli["backend"].submissions == 1

    assert cli["run"]("--config", str(cfg), "--input", str(source),
                      "--force") == 0
    assert cli["backend"].submissions == 2


def test_no_registry_restores_legacy_path(cli):
    source = _write_input(cli)

    assert cli["run"]("--no-registry", "--input", str(source)) == 0
    assert cli["backend"].submissions == 1
    # No Registry database is created anywhere in the tmp workspace.
    assert not (cli["tmp_path"] / "factor_registry.db").exists()
    assert not (cli["tmp_path"] / "data" / "factor_registry.db").exists()
