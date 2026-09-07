from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_owner_style as style  # noqa: E402
import vod_montage_cluster as cluster  # noqa: E402


def test_style_reference_from_hardcoded_vod() -> None:
    vod = Path("/tmp/yt_Tovruh33adY.mp4")
    refs = style.style_reference_peaks(vod)
    assert 5266.0 in refs
    avoid = style.style_avoid_peaks(vod)
    assert 1533.0 in avoid

    vod2 = Path("/tmp/yt_bMn-6uTsDBg.mp4")
    assert 243.0 in style.style_reference_peaks(vod2)
    assert 141.0 in style.style_avoid_peaks(vod2)

    vod3 = Path("/tmp/yt_Z7wR4vZkn5E.mp4")
    assert 1164.0 in style.style_reference_peaks(vod3)

    vod4 = Path("/tmp/yt_FxTv16VoLZk.mp4")
    assert 30.0 in style.style_reference_peaks(vod4)


def test_cluster_prefers_anchor_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SEQUENTIAL", "1")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC", "240")
    rows = [
        {"peak_start": 5200.0, "start": 5195.0, "score": 0.70, "segment_id": "a"},
        {"peak_start": 5240.0, "start": 5235.0, "score": 0.72, "segment_id": "b"},
        {"peak_start": 5266.0, "start": 5261.0, "score": 0.95, "segment_id": "c"},
        {"peak_start": 1500.0, "start": 1495.0, "score": 0.99, "segment_id": "d"},
        {"peak_start": 1530.0, "start": 1525.0, "score": 0.98, "segment_id": "e"},
    ]
    picked = cluster.pick_sequential_montage_rows(
        rows,
        min_clips=2,
        max_clips=2,
        anchor_peaks=[5266.0],
    )
    peaks = [float(r["peak_start"]) for r in picked[:2]]
    assert min(peaks) >= 5200.0
    assert max(peaks) <= 5300.0


def test_rank_peaks_by_style_orders_toward_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    vod = Path("/tmp/yt_Tovruh33adY.mp4")
    ref_row = {
        "gunfire_density": 0.09,
        "panns_gun_max": 0.42,
        "notification_score": 0.82,
        "payoff_fast": 0.88,
        "fight_fast": 0.75,
        "notification_hit": True,
        "fast_score": 0.9,
        "loot_walk": False,
    }
    near_row = dict(ref_row)
    far_row = {
        "gunfire_density": 0.02,
        "panns_gun_max": 0.10,
        "notification_score": 0.05,
        "payoff_fast": 0.08,
        "fight_fast": 0.12,
        "notification_hit": False,
        "fast_score": 0.8,
        "loot_walk": True,
    }
    with patch.object(style, "style_reference_peaks", return_value=[5266.0]), patch.object(
        style, "build_style_profile", return_value=ref_row
    ), patch("pubg_fast_peak_rank.score_peak_fast", side_effect=lambda _v, peak, **_: near_row if peak > 5000 else far_row):
        ranked, reason, sims = style.rank_peaks_by_style(
            vod,
            [1533.0, 5266.0, 5288.0],
            part_sec=14.0,
        )
    assert ranked[0] >= 5200.0
    assert "style_rank" in reason
    assert sims[float(ranked[0])] >= sims[float(1533.0)]
