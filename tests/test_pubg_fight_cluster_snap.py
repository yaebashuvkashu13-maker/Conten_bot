"""Short fight cluster snap — #3hDKNrY4sGU_580 style (50s → ~28–35s burst)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _timeline_580_style() -> list[dict]:
    """Synthesize the measured clip: early fight, quiet run, hot 28–35s burst."""
    rows = []
    for t in range(0, 50):
        if 1 <= t <= 9:
            gun, rms = 0.18, 0.22
        elif 28 <= t <= 35:
            gun, rms = 0.20, 0.30  # hotter than early burst
        elif 11 <= t <= 27:
            gun, rms = 0.05, 0.003  # false-gun quiet run
        else:
            gun, rms = 0.02, 0.004
        rows.append({"start": float(t), "gun": gun, "rms": rms, "score": gun})
    return rows


def test_snap_keeps_hot_28_35_burst_not_full_50s() -> None:
    from pubg_montage_bounds import snap_to_best_fight_cluster

    report = {"timeline": _timeline_580_style()}
    # Peak sits on the early fight (as in ledger peak=588 → +7.5s).
    start, dur = snap_to_best_fight_cluster(0.0, 50.0, report, peak=7.5, max_cluster_sec=12.0)
    assert start >= 26.0
    assert start <= 30.0
    assert start + dur <= 40.0
    assert 5.0 <= dur <= 12.0


def test_extend_past_active_gun_covers_full_audible_burst() -> None:
    """Owner: DGso@447 10s cut ended mid-burst — extend while RMS/gun still hot."""
    from pubg_montage_bounds import extend_end_past_active_gunfire

    timeline = []
    for t in range(447, 465):
        # Match measured RMS: loud 452–460, quiet after.
        if 452 <= t <= 460:
            gun, rms = 0.12, 0.25
        elif t in (448,):
            gun, rms = 0.08, 0.07
        else:
            gun, rms = 0.02, 0.008
        timeline.append({"start": float(t), "gun": gun, "rms": rms})
    report = {"timeline": timeline}
    # Short 10s window ending mid-fight (457) must grow through 460.
    start, dur = extend_end_past_active_gunfire(447.0, 10.0, report, max_dur=22.0, single=True)
    assert start == 447.0
    assert start + dur >= 461.0
    assert dur <= 22.0


def test_trim_edges_then_snap_drops_run_bridge() -> None:
    from pubg_montage_bounds import trim_quiet_run_edges

    report = {"timeline": _timeline_580_style()}
    start, dur = trim_quiet_run_edges(0.0, 50.0, report, max_dur=50.0)
    assert dur <= 14.0
    assert start >= 25.0


def test_silent_false_gun_not_active() -> None:
    from pubg_fight_segment import _gunfire_active_flags

    timeline = [
        {"start": 0.0, "gun": 0.06, "rms": 0.002},
        {"start": 2.0, "gun": 0.08, "rms": 0.25},
        {"start": 4.0, "gun": 0.05, "rms": 0.003},
    ]
    flags = _gunfire_active_flags(timeline, 0.34)
    assert flags == [False, True, False]


def test_owner_neighborhood_defaults_are_short() -> None:
    src = (SCRIPTS / "shooter_owner_montage.py").read_text(encoding="utf-8")
    assert 'PUBG_OWNER_NEIGHBORHOOD_MIN_DUR_SEC", "6"' in src
    assert 'PUBG_OWNER_NEIGHBORHOOD_MAX_DUR_SEC", "18"' in src
    assert 'PUBG_OWNER_NEIGHBORHOOD_DUR_SEC", "14"' in src


def test_tighten_prefers_hot_burst_over_early_shoot_start() -> None:
    from pubg_montage_bounds import tighten_pubg_clip_bounds

    report = {
        "timeline": _timeline_580_style(),
        "shooting_start": 1.0,
        "kill_sec": 33.0,
        "fight_end": 35.0,
    }
    start, dur = tighten_pubg_clip_bounds(0.0, 50.0, report, peak=7.5, single=True)
    assert start >= 26.0
    assert start + dur <= 40.0
    assert dur <= 14.0
