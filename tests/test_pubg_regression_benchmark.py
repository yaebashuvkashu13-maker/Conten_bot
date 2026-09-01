from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_regression_benchmark import (  # noqa: E402
    _labels,
    _nearest,
    aggregate,
    apply_online_overrides,
    rescore_vod_result,
)


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


def test_immutable_regression_set_has_all_18_timestamps() -> None:
    labels = _labels(Path(__file__).resolve().parent.parent / "data" / "pubg_regression_labels.json")
    assert set(labels) == {"pJ-X6NdSU9k", "zv3JymSZOb0", "n97cHIR9Qow"}
    assert sum(len(rows) for rows in labels.values()) == 18


def test_online_feedback_overrides_but_preserves_immutable_label(tmp_path: Path) -> None:
    online = tmp_path / "online.json"
    online.write_text(
        '{"videos":{"abc":[{"time_sec":100,"label":"bad"}]}}',
        encoding="utf-8",
    )
    adjusted, conflicts = apply_online_overrides(
        {"abc": [{"time_sec": 100, "label": "good"}]},
        online,
    )
    assert adjusted["abc"][0]["label"] == "bad"
    assert adjusted["abc"][0]["immutable_label"] == "good"
    assert len(conflicts) == 1


def test_rescore_existing_report_without_video_analysis() -> None:
    result = {
        "video_id": "abc",
        "peaks": [100.0, 200.0],
        "ranked_peaks": [200.0, 100.0],
        "quality": [
            {"peak": 100.0, "accepted": True},
            {"peak": 200.0, "accepted": False},
        ],
    }
    rescored = rescore_vod_result(
        result,
        [{"time_sec": 100, "label": "good"}, {"time_sec": 200, "label": "bad"}],
        tolerance=10,
    )
    assert rescored["good_accepted_hits"] == 1
    assert rescored["bad_accepted_hits"] == 0
