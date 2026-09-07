from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vod_montage_cluster as cluster  # noqa: E402


def _row(peak: float, score: float) -> dict:
    return {"peak_start": peak, "start": peak - 5, "score": score, "segment_id": f"seg_{int(peak)}"}


def test_sequential_picks_nearby_fights_not_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SEQUENTIAL", "1")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC", "180")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_PART_GAP_SEC", "20")
    rows = [
        _row(100.0, 0.95),
        _row(130.0, 0.90),
        _row(160.0, 0.88),
        _row(2500.0, 0.99),
        _row(2530.0, 0.97),
    ]
    picked = cluster.pick_montage_rows(rows, min_clips=2, max_clips=2, gap_sec=55)
    peaks = [float(r["peak_start"]) for r in picked[:2]]
    assert len(peaks) == 2
    assert max(peaks) - min(peaks) <= 180
    assert peaks == sorted(peaks)


def test_sequential_prefers_higher_scoring_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SEQUENTIAL", "1")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC", "200")
    rows = [
        _row(500.0, 0.70),
        _row(530.0, 0.72),
        _row(1500.0, 0.95),
        _row(1530.0, 0.94),
        _row(1560.0, 0.93),
    ]
    picked = cluster.pick_montage_rows(rows, min_clips=2, max_clips=2, gap_sec=55)
    peaks = [float(r["peak_start"]) for r in picked[:2]]
    assert min(peaks) >= 1500.0


def test_spread_mode_when_sequential_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SEQUENTIAL", "0")
    rows = [
        _row(100.0, 0.80),
        _row(130.0, 0.79),
        _row(2500.0, 0.99),
    ]
    picked = cluster.pick_montage_rows(rows, min_clips=2, max_clips=2, gap_sec=55)
    peaks = [float(r["peak_start"]) for r in picked[:2]]
    assert 2500.0 in peaks


def test_sequential_pool_keeps_chronological_order() -> None:
    scored = [
        (0.9, 100.0),
        (0.85, 125.0),
        (0.99, 2000.0),
        (0.88, 2025.0),
        (0.87, 2050.0),
    ]
    peaks = cluster.sequential_pool_peaks(scored, pool_cap=5, part_gap_sec=20)
    assert peaks == sorted(peaks)
    assert len(peaks) == 5
    assert peaks[0] == 100.0 and peaks[-1] == 2050.0
