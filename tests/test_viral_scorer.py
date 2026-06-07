from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import HighlightMetrics  # noqa: E402
from viral_scorer import hook_score_frame, montage_viral_score, segment_viral_score  # noqa: E402


def test_menu_frame_low_hook_score(monkeypatch) -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[200:600, 300:900] = 40
    monkeypatch.setattr(
        "visual_action_check.check_frame_visual",
        lambda _p, _f: (False, "no_visible_combat", {}),
    )
    score, meta = hook_score_frame(frame, "pubg")
    assert score < 0.35


def test_segment_viral_score_uses_hook() -> None:
    m = HighlightMetrics(
        start=100,
        duration=10,
        profile="pubg",
        panns_gun_max=0.4,
        clip_score=0.1,
        center_motion=0.2,
        hook_score=0.8,
        heatmap_intensity=0.5,
    )
    assert segment_viral_score(m) > 0.12


def test_montage_requires_first_segment_hook() -> None:
    low_hook = {
        "highlight_metrics": {"hook_score": 0.2, "viral_score": 0.5, "combined_score": 0.5},
        "start": 100,
    }
    high_hook = {
        "highlight_metrics": {"hook_score": 0.8, "viral_score": 0.6, "combined_score": 0.6},
        "start": 500,
    }
    _, ok_low = montage_viral_score([low_hook, high_hook])
    _, ok_high = montage_viral_score([high_hook, low_hook])
    assert ok_low is False
    assert ok_high is True
