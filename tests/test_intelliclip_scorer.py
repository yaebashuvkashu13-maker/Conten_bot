from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from intelliclip_scorer import (  # noqa: E402
    candidate_intelliclip_score,
    intelliclip_score,
    merge_starts_with_anchors,
    select_intelliclip_clips,
    window_bin_signals,
)


def test_intelliclip_score_prefers_gun_and_hook() -> None:
    weak = {"gun_peak": 0.05, "hook_energy": 0.04, "visual_dynamics": 0.1, "gun_sustain": 0.1, "audio_peak": 0.05}
    strong = {"gun_peak": 0.55, "hook_energy": 0.48, "visual_dynamics": 0.4, "gun_sustain": 0.5, "audio_peak": 0.3}
    assert intelliclip_score(strong, "pubg") > intelliclip_score(weak, "pubg")


def test_merge_starts_boosts_owner_anchors() -> None:
    ranked = [(900.0, 0.35, "a"), (1200.0, 0.40, "b")]
    merged = merge_starts_with_anchors(ranked, [515.0, 2842.0], limit=10)
    assert 510.0 in merged
    assert 2837.0 in merged
    assert merged.index(510.0) < merged.index(900.0)


def test_anchor_proximity_boosts_ranking() -> None:
    near = {"start": 510.0, "highlight_metrics": {"intelliclip_score": 0.55, "hook_score": 0.4}}
    far = {"start": 7000.0, "highlight_metrics": {"intelliclip_score": 0.70, "hook_score": 0.5}}
    assert candidate_intelliclip_score(near, [515.0]) > candidate_intelliclip_score(far, [515.0])


def test_select_intelliclip_clips_respects_gap_and_cap() -> None:
    pool = [
        {"start": 100, "output_duration": 10, "highlight_metrics": {"intelliclip_score": 0.9, "hook_score": 0.8}},
        {"start": 120, "output_duration": 10, "highlight_metrics": {"intelliclip_score": 0.85, "hook_score": 0.7}},
        {"start": 400, "output_duration": 10, "highlight_metrics": {"intelliclip_score": 0.7, "hook_score": 0.6}},
        {"start": 900, "output_duration": 10, "highlight_metrics": {"intelliclip_score": 0.65, "hook_score": 0.5}},
    ]
    chosen = select_intelliclip_clips(pool, video_duration=3600, max_clips=3, min_gap=75.0)
    starts = [c["start"] for c in chosen]
    assert 100 in starts
    assert 120 not in starts
    assert len(chosen) <= 3


def test_window_bin_signals_from_mock_analysis() -> None:
    analysis = {
        "window_seconds": 2.0,
        "duration": 120.0,
        "audio": np.linspace(0, 1, 60, dtype=np.float32),
        "gunfire": np.linspace(0, 0.8, 60, dtype=np.float32),
        "center_motion": np.linspace(0, 0.5, 60, dtype=np.float32),
        "scene": np.zeros(60, dtype=np.float32),
    }
    sig = window_bin_signals(analysis, 40.0, 10.0, "pubg")
    assert sig["gun_peak"] > 0.2
    assert "hook_energy" in sig
