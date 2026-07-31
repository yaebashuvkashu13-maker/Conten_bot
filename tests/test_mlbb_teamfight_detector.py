"""Tests for MLBB teamfight detector."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_teamfight_detector import (  # noqa: E402
    banner_tier_weight,
    combined_teamfight_score,
    passes_teamfight_threshold,
    score_teamfight_bins,
)


def test_teamfight_bins_high_motion_scores() -> None:
    analysis = {
        "window_seconds": 2.0,
        "center_motion": np.linspace(0.01, 0.9, 120, dtype=np.float32),
        "audio": np.linspace(0.01, 0.5, 120, dtype=np.float32),
    }
    low = score_teamfight_bins(analysis, 10.0)
    high = score_teamfight_bins(analysis, 200.0)
    assert high > low


def test_rank_starts_does_not_fallback_to_all_junk(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_TEAMFIGHT_MIN_SCORE", "0.45")
    monkeypatch.setenv("MLBB_TEAMFIGHT_RANK_FRAC", "0.75")
    monkeypatch.setenv("MLBB_TEAMFIGHT_ABS_FLOOR", "0.90")  # force empty
    monkeypatch.setenv("MLBB_TEAMFIGHT_HUD", "0")
    from mlbb_teamfight_detector import rank_starts_by_teamfight

    analysis = {
        "window_seconds": 2.0,
        "center_motion": np.full(80, 0.02, dtype=np.float32),
        "audio": np.full(80, 0.01, dtype=np.float32),
    }
    starts = [10.0, 40.0, 80.0, 120.0]
    assert rank_starts_by_teamfight(analysis, starts) == []


def test_metrics_combat_score_prefers_real_fights() -> None:
    from mlbb_teamfight_detector import metrics_combat_score

    farm = metrics_combat_score(center_motion=0.02, minimap_delta=0.01, skill_delta=0.005)
    fight = metrics_combat_score(center_motion=0.08, minimap_delta=0.04, skill_delta=0.03)
    assert fight > farm
    assert fight >= 0.85
    assert farm < 0.85


def test_combined_score_banner_tier_boost() -> None:
    analysis = {
        "window_seconds": 2.0,
        "center_motion": np.full(120, 0.6, dtype=np.float32),
        "audio": np.full(120, 0.3, dtype=np.float32),
    }
    base = combined_teamfight_score(analysis, 100.0, banner_tier=1)
    boosted = combined_teamfight_score(analysis, 100.0, banner_tier=3)
    assert boosted > base
    assert banner_tier_weight("double") == 0.55
    assert passes_teamfight_threshold(boosted) or not passes_teamfight_threshold(0.1)
