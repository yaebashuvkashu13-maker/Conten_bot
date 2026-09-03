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
        peak=1164.0,
    )
    assert start >= 1150.5
    assert start + dur <= 1167.0 + 0.01


def test_single_tighten_keeps_full_fight_not_kill_tail():
    """ACCvn55IvVw: single mode must not crush 52s fight into ~10s running tail."""
    report = {
        "shooting_start": 118.0,
        "kill_sec": 125.0,
        "fight_end": 170.5,
    }
    start, dur = tighten_pubg_clip_bounds(
        118.0,
        52.5,
        report,
        peak=128.9,
        single=True,
    )
    assert dur >= 20.0
    assert start <= 119.0
    assert start + dur >= 128.9


def test_extend_past_gunfire_does_not_end_mid_burst():
    """#6tBEG4XXXP8_1065: clip ended at 1089 while gunfire continued to ~1114."""
    from pubg_montage_bounds import clip_ends_on_gunfire, extend_end_past_active_gunfire

    timeline = []
    for t, gun in (
        (1064, 0.04),
        (1072, 0.08),
        (1076, 0.14),
        (1078, 0.01),
        (1081, 0.01),
        (1088, 0.05),
        (1090, 0.03),
        (1094, 0.08),
        (1098, 0.09),
        (1104, 0.05),
        (1110, 0.05),
        (1114, 0.02),
        (1116, 0.0),
        (1118, 0.0),
        (1120, 0.0),
    ):
        timeline.append({"start": float(t), "gun": float(gun), "score": float(gun) * 10})
    report = {
        "shooting_start": 1064.0,
        "kill_sec": 1081.0,
        "fight_end": 1120.5,
        "timeline": timeline,
    }
    assert clip_ends_on_gunfire(1065.2, 24.5, report) is True
    start, dur = extend_end_past_active_gunfire(
        1065.2, 24.5, report, max_dur=90.0, single=True
    )
    assert start + dur >= 1114.0
    assert clip_ends_on_gunfire(start, dur, report) is False


def test_tighten_single_extends_mid_burst_cut():
    timeline = []
    for t in range(1064, 1122, 2):
        gun = 0.06 if t < 1114 else 0.0
        timeline.append({"start": float(t), "gun": gun, "score": 0.7 if gun else 0.05})
    report = {
        "shooting_start": 1064.0,
        "kill_sec": 1081.0,
        "fight_end": 1120.5,
        "timeline": timeline,
    }
    start, dur = tighten_pubg_clip_bounds(
        1065.2,
        24.5,
        report,
        peak=1078.0,
        single=True,
    )
    assert start + dur >= 1112.0
    assert dur >= 40.0


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
