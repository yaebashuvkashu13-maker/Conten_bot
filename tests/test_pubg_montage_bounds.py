#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_montage_bounds import (  # noqa: E402
    bounds_distinct,
    dedupe_peaks_by_fight_window,
    tighten_pubg_clip_bounds,
)


def test_bounds_distinct_requires_gap_or_no_overlap():
    assert bounds_distinct((238.0, 262.0), (328.0, 352.0)) is True
    assert bounds_distinct((238.0, 262.0), (276.0, 300.0)) is False
    assert bounds_distinct((225.0, 249.0), (238.0, 262.0)) is False


def test_tighten_pubg_clip_trims_loot_tail():
    start, dur = tighten_pubg_clip_bounds(
        1156.0,
        26.5,
        {"shooting_start": 1152.0, "kill_sec": 1162.0, "fight_end": 1182.5},
    )
    assert start == 1150.5
    assert start + dur <= 1167.0 + 0.01


def test_dedupe_peaks_by_fight_window_drops_same_fight(monkeypatch):
    vod = Path("yt_bMn-6uTsDBg.mp4")
    windows = {
        231.0: (225.0, 249.0),
        243.0: (238.0, 262.0),
        334.0: (328.0, 352.0),
    }

    def fake_bounds(_vod, peak, file_dur=None):
        return windows[float(peak)]

    monkeypatch.setattr("pubg_montage_bounds.fight_bounds", fake_bounds)
    out = dedupe_peaks_by_fight_window(vod, [231.0, 243.0, 334.0])
    assert 334.0 in out
    assert len(out) == 2
    assert not (231.0 in out and 243.0 in out)
