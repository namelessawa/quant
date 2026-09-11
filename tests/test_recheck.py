"""``scripts/recheck_submissions.py``: which stored alphas get re-checked.

The script itself only issues reads, so the interesting logic is the selection:
re-checking all 100+ stored rows when one was asked for would burn requests
against an aggressive rate limiter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import submission_checks as all_pass_checks
from worldquant.api import SimulationStatus
from worldquant.config import ConfigError
from worldquant.models import AlphaResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import recheck_submissions  # noqa: E402


def make(alpha_id, *, grade="GOOD", checks=None, status=SimulationStatus.COMPLETED):
    return AlphaResult(
        alpha_id=alpha_id, expression=f"rank({alpha_id})", dedup_key=alpha_id,
        status=status, remote_alpha_id=alpha_id, grade=grade, submission_checks=checks,
    )


class TestGradeAtLeast:
    def test_no_floor_accepts_everything(self):
        assert recheck_submissions.grade_at_least(None, None) is True
        assert recheck_submissions.grade_at_least("INFERIOR", None) is True

    def test_the_floor_is_inclusive(self):
        assert recheck_submissions.grade_at_least("GOOD", "GOOD") is True
        assert recheck_submissions.grade_at_least("SPECTACULAR", "GOOD") is True

    def test_below_the_floor_is_rejected(self):
        assert recheck_submissions.grade_at_least("AVERAGE", "GOOD") is False

    def test_a_missing_grade_never_qualifies(self):
        assert recheck_submissions.grade_at_least(None, "GOOD") is False
        assert recheck_submissions.grade_at_least("", "INFERIOR") is False


class TestSelectTargets:
    def test_unchecked_rows_are_selected(self):
        targets = recheck_submissions.select_targets(
            [make("A1"), make("A2")], min_grade=None, alpha_ids=None, include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A1", "A2"]

    def test_already_verified_rows_are_skipped_by_default(self):
        targets = recheck_submissions.select_targets(
            [make("A1", checks=all_pass_checks()), make("A2")],
            min_grade=None, alpha_ids=None, include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A2"]

    def test_all_refreshes_verified_rows_too(self):
        # SELF_CORRELATION drifts as the account grows, so a refresh must be
        # able to overwrite a verdict that already passed.
        targets = recheck_submissions.select_targets(
            [make("A1", checks=all_pass_checks()), make("A2")],
            min_grade=None, alpha_ids=None, include_verified=True,
        )
        assert [t.remote_alpha_id for t in targets] == ["A1", "A2"]

    def test_min_grade_filters(self):
        rows = [make("A1", grade="INFERIOR"), make("A2", grade="GOOD"),
                make("A3", grade="SPECTACULAR")]
        targets = recheck_submissions.select_targets(
            rows, min_grade="GOOD", alpha_ids=None, include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A2", "A3"]

    def test_explicit_ids_filter(self):
        rows = [make("A1"), make("A2"), make("A3")]
        targets = recheck_submissions.select_targets(
            rows, min_grade=None, alpha_ids=["A3", "A1"], include_verified=False,
        )
        assert {t.remote_alpha_id for t in targets} == {"A1", "A3"}

    def test_explicit_ids_bypass_the_grade_floor(self):
        targets = recheck_submissions.select_targets(
            [make("A1", grade="INFERIOR")], min_grade="GOOD", alpha_ids=["A1"],
            include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A1"]

    def test_explicit_ids_bypass_the_already_verified_skip(self):
        targets = recheck_submissions.select_targets(
            [make("A1", checks=all_pass_checks()), make("A2")],
            min_grade=None, alpha_ids=["A1"], include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A1"]

    def test_an_unknown_explicit_id_is_an_error_not_a_silent_no_op(self):
        with pytest.raises(ConfigError, match="NOPE"):
            recheck_submissions.select_targets(
                [make("A1")], min_grade=None, alpha_ids=["NOPE"], include_verified=False,
            )

    def test_rows_without_a_remote_id_are_skipped(self):
        orphan = make("A1")
        orphan.remote_alpha_id = None
        targets = recheck_submissions.select_targets(
            [orphan, make("A2")], min_grade=None, alpha_ids=None, include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A2"]

    def test_one_row_per_alpha(self):
        targets = recheck_submissions.select_targets(
            [make("A1"), make("A1"), make("A2")],
            min_grade=None, alpha_ids=None, include_verified=False,
        )
        assert [t.remote_alpha_id for t in targets] == ["A1", "A2"]

    def test_nothing_to_do_is_an_empty_list(self):
        assert recheck_submissions.select_targets(
            [], min_grade=None, alpha_ids=None, include_verified=True,
        ) == []
