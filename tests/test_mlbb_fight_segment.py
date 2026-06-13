from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_fight_segment import detect_fight_bounds, variable_length_enabled  # noqa: E402


def test_variable_length_enabled_default(monkeypatch) -> None:
    monkeypatch.delenv("MLBB_VOD_VARIABLE_LENGTH", raising=False)
    assert variable_length_enabled() is True


def test_detect_fight_bounds_clamps(monkeypatch, tmp_path: Path) -> None:
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"x")
    bins = 600
    analysis = {
        "window_seconds": 2.0,
        "duration": 1200.0,
        "bins": bins,
        "center_motion": np.linspace(0.01, 0.5, bins).tolist(),
        "audio": np.linspace(0.01, 0.4, bins).tolist(),
        "scene": np.linspace(0.01, 0.3, bins).tolist(),
    }
    fake = MagicMock()
    fake.analyze_video = MagicMock(return_value=analysis)
    monkeypatch.setitem(sys.modules, "smart_video_editor", fake)

    start, end, dur = detect_fight_bounds(vod, 600.0)
    assert 7.0 <= dur <= 22.0
    assert start >= 0
    assert end <= 1200.0
