#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_clip_shape_gate import validate_clip_fight_shape  # noqa: E402
from pubg_montage_bounds import tighten_pubg_clip_bounds  # noqa: E402


def test_reject_prefight_running():
    ok, reason = validate_clip_fight_shape(
        376.0,
        40.0,
        389.5,
        {"shooting_start": 388.0, "fight_end": 402.0},
    )
    assert not ok
    assert "prefight" in reason


def test_reject_fight_at_end():
    ok, reason = validate_clip_fight_shape(
        1205.0,
        20.0,
        1221.2,
        {"shooting_start": 1207.0, "fight_end": 1224.0},
    )
    assert not ok
    assert "fight_at_end" in reason


def test_accept_tight_gunfight():
    ok, reason = validate_clip_fight_shape(
        385.0,
        14.0,
        389.5,
        {
            "shooting_start": 386.0,
            "kill_sec": 392.0,
            "fight_end": 398.0,
            "timeline": [
                {"start": 384.0, "gun": 0.04, "score": 0.5},
                {"start": 386.0, "gun": 0.08, "score": 0.7},
                {"start": 388.0, "gun": 0.09, "score": 0.8},
                {"start": 390.0, "gun": 0.05, "score": 0.6},
            ],
        },
    )
    assert ok, reason


def test_tighten_trims_running_lead():
    start, dur = tighten_pubg_clip_bounds(
        376.0,
        40.0,
        {"shooting_start": 388.0, "kill_sec": 392.0, "fight_end": 395.0},
        peak=389.5,
    )
    # pre-shoot ~0.8s — cut run-up, keep contact near open.
    assert start >= 386.8
    assert dur >= 6.0
    assert start <= 387.3


def test_reject_loot_tail():
    ok, reason = validate_clip_fight_shape(
        100.0,
        16.0,
        104.0,
        {"shooting_start": 100.5, "fight_end": 108.0},
    )
    assert not ok
    assert "loot_tail" in reason


def test_reject_head_run_edge():
    ok, reason = validate_clip_fight_shape(
        200.0,
        14.0,
        208.0,
        {
            "shooting_start": 206.0,
            "fight_end": 213.0,
            "timeline": [
                {"start": 200.0, "gun": 0.005, "score": 0.1},
                {"start": 202.0, "gun": 0.008, "score": 0.1},
                {"start": 206.0, "gun": 0.09, "score": 0.8},
                {"start": 208.0, "gun": 0.08, "score": 0.7},
                {"start": 210.0, "gun": 0.06, "score": 0.6},
            ],
        },
    )
    assert not ok
    assert "head_run" in reason or "prefight" in reason
