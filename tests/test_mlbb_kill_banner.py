#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_kill_banner import (  # noqa: E402
    bounds_from_banner,
    classify_banner_text,
)


def test_classify_savage_and_triple() -> None:
    s = classify_banner_text("SAVAGE!")
    assert s is not None
    assert s.tier == 5
    assert s.label == "savage"
    t = classify_banner_text("TRIPLE KILL")
    assert t is not None
    assert t.tier == 3
    assert t.label == "triple"
    m = classify_banner_text("MANIAC")
    assert m is not None
    assert m.tier == 4


def test_reject_single_kill_only() -> None:
    single = classify_banner_text("You got a Kill")
    assert single is not None
    assert single.tier == 1


def test_bounds_from_fight_sustain() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "32"
    start, end, dur = bounds_from_banner(
        100.0,
        file_dur=200.0,
        fight_start=88.0,
        fight_end=118.0,
    )
    assert start == 88.0
    assert end == 116.0
    assert dur == 28.0


def test_bounds_fallback_without_fight() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    start, end, dur = bounds_from_banner(50.0, file_dur=120.0)
    assert start == 46.0
    assert 8.0 <= dur <= 28.0
