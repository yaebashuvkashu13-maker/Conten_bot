#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_teamfight_detector import fight_first_peaks  # noqa: E402


def _fake_analysis(*, hot: list[int], cold: list[int], win: float = 2.0) -> dict:
    n = max([*hot, *cold, 0]) + 5
    motion = np.full(n, 0.01, dtype=np.float32)
    audio = np.full(n, 0.01, dtype=np.float32)
    for i in hot:
        motion[i] = 0.12
        audio[i] = 0.20
        # sustain a couple bins
        if i + 1 < n:
            motion[i + 1] = 0.10
            audio[i + 1] = 0.15
    for i in cold:
        motion[i] = 0.02
        audio[i] = 0.02
    return {
        "window_seconds": win,
        "center_motion": motion,
        "audio": audio,
    }


def test_fight_first_ranks_hot_peaks_above_cold(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_FIGHT_FIRST_MIN_SCORE", "0.20")
    monkeypatch.setenv("MLBB_BANNER_FIGHT_FIRST_PEAKS", "4")
    monkeypatch.setenv("MLBB_TEAMFIGHT_ABS_MOTION", "0.03")
    analysis = _fake_analysis(hot=[20, 40], cold=[10, 30])
    starts = [20.0, 40.0, 60.0, 80.0]  # bins 10,20,30,40
    ranked = fight_first_peaks(analysis, starts, limit=2)
    assert ranked
    # Hottest fight bins should win.
    assert ranked[0] in (40.0, 80.0)


def test_fight_first_derives_peaks_from_analysis(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_FIGHT_FIRST_MIN_SCORE", "0.15")
    monkeypatch.setenv("MLBB_BANNER_FIGHT_FIRST_PEAKS", "3")
    monkeypatch.setenv("MLBB_VOD_MIN_PEAK_SEC", "10")
    monkeypatch.setenv("MLBB_FIGHT_FIRST_MIN_GAP_SEC", "8")
    monkeypatch.setenv("MLBB_TEAMFIGHT_ABS_MOTION", "0.03")
    analysis = _fake_analysis(hot=[15, 30], cold=[5])
    ranked = fight_first_peaks(analysis, None, limit=2)
    assert len(ranked) >= 1
    assert all(t >= 10.0 for t in ranked)
