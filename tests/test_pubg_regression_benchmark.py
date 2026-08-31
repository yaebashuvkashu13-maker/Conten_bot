from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_regression_benchmark import _nearest, aggregate  # noqa: E402


def test_nearest_candidate_uses_tolerance() -> None:
    assert _nearest([90.0, 140.0], 100.0, 15.0) == 90.0
    assert _nearest([90.0, 140.0], 120.0, 15.0) is None


def test_aggregate_owner_regression_metrics() -> None:
    summary = aggregate(
        [
            {
                "good_total": 4,
                "good_generator_hits": 3,
                "good_top10_hits": 2,
                "good_accepted_hits": 2,
                "bad_total": 2,
                "bad_accepted_hits": 1,
            }
        ]
    )
    assert summary["generator_recall"] == 0.75
    assert summary["ranker_recall_at_10"] == 0.5
    assert summary["accepted_recall"] == 0.5
    assert summary["bad_accept_rate"] == 0.5
